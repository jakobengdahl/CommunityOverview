"""
The hardened-subprocess runner and the run loop that joins it to the broker
(docs/adr/0012-user-function-isolation.md, docs/USER_FUNCTION_CONTRACT.md §3,
§8, §11).

The runner seam is ``probe`` / ``start`` / ``kill`` / ``wait`` (ADR 0012 §3).
``HardenedSubprocessRunner`` starts each run through ``sandbox.py``, a per-run
supervisor process that confines the function and terminates everything it
started. ``run_function`` drives one run end to end: start, protocol session,
termination, and the mapping of what happened onto the §11 status and code.

A runner never retries: a run is executed at most once.
"""

from __future__ import annotations

import json
import logging
import os
import select
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Tuple

from backend.functions import sandbox
from backend.runtime.authorization import use_request_authorization
from backend.runtime.extensions import (
    AdmissionRefused,
    bind_execution_context,
    bind_runtime_extensions,
    get_runtime_extensions,
    runtime_context,
)
from backend.functions.broker import (
    FunctionBroker,
    ToolCallRecord,
    _authorization_kwargs,
)
from backend.functions.protocol import (
    CODE_NO_RESULT,
    CODE_PROTOCOL_ERROR,
    CODE_TIMEOUT,
    MAX_OUTPUT_BYTES,
    SessionOutcome,
    run_session,
)

logger = logging.getLogger(__name__)

CONTROLS = ("filesystem", "network", "processes", "isolation")
LEVEL_FULL = "full"
LEVEL_DEGRADED = "degraded"

RUNTIME_PYTHON = "python"

MAX_TIMEOUT_SECONDS = 120
MAX_MEMORY_MIB = 256
MAX_LOG_BYTES = 256 * 1024
FILE_SIZE_MIB = 16
MAX_DISK_BYTES = 256 * 1024 * 1024
MAX_DISK_FILES = 10_000

# Run statuses and the run-level codes this module produces (§11).
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"
CODE_RUNNER_LOST = "runner_lost"
CODE_DISK_EXCEEDED = "disk_exceeded"
CODE_DISK_CHECK_FAILED = "disk_check_failed"

# How long a function that sent its result may take to exit on its own, and how
# long the supervisor gets to terminate everything before it counts as lost.
EXIT_GRACE_SECONDS = 1.0
STOP_TIMEOUT_SECONDS = 10.0

_SANDBOX = os.path.abspath(sandbox.__file__)
HELPER_LIBRARY_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "runtime", "python"
)

# Runs the entry point with only the function's directory and the helper
# library on the module path (§10). Paths arrive as arguments, never as code.
_PYTHON_BOOTSTRAP = (
    "import runpy, sys\n"
    "fn, helper, entry = sys.argv[1:4]\n"
    "sys.path[:] = [fn, helper] + [p for p in sys.path if p]\n"
    "sys.argv = [entry]\n"
    "runpy.run_path(entry, run_name='__main__')\n"
)


class RunnerUnavailable(RuntimeError):
    """The host cannot start confined processes at all."""


@dataclass(frozen=True)
class RunnerProbe:
    level: str
    missing: Tuple[str, ...]
    runtimes: Tuple[str, ...]


@dataclass(frozen=True)
class RunSpec:
    """What one run executes. ``files`` holds the function's own files as
    content, keyed by path relative to the function directory."""

    run_id: str
    runtime: str
    entry_point: str
    files: Mapping[str, bytes]
    timeout_seconds: float = MAX_TIMEOUT_SECONDS
    memory_mib: int = MAX_MEMORY_MIB
    max_log_bytes: int = MAX_LOG_BYTES

    def __post_init__(self) -> None:
        # The platform maximums are absolute (§8): no caller can raise them.
        for name, value, maximum in (
            ("timeout_seconds", self.timeout_seconds, MAX_TIMEOUT_SECONDS),
            ("memory_mib", self.memory_mib, MAX_MEMORY_MIB),
            ("max_log_bytes", self.max_log_bytes, MAX_LOG_BYTES),
        ):
            if isinstance(value, bool) or not 0 < value <= maximum:
                raise ValueError(f"{name} must be above 0 and at most {maximum}")


@dataclass
class RunExit:
    """How the process ended. ``outcome`` is ``exited``, ``killed``,
    ``runner_lost`` or ``failed_to_start``."""

    outcome: str
    exit_code: Optional[int] = None
    signal: Optional[int] = None
    peak_memory_kib: Optional[int] = None
    wall_ms: int = 0
    log: str = ""
    log_truncated: bool = False
    detail: str = ""
    cpu_limit_reached: bool = False
    disk_limit_reached: bool = False
    disk_check_failed: bool = False


def _controls_for(landlock_abi: int, seccomp: bool) -> Tuple[str, ...]:
    held = []
    if landlock_abi >= sandbox.FILESYSTEM_MIN_ABI:
        held.append("filesystem")
    if seccomp:
        held += ["network", "processes"]
        if landlock_abi >= sandbox.ISOLATION_MIN_ABI:
            held.append("isolation")
    return tuple(held)


def safe_relative_path(path: str) -> str:
    """``path`` as a normalized relative path that stays inside its directory."""
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        raise ValueError(f"invalid path: {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in ("..", "") for part in pure.parts):
        raise ValueError(f"path leaves the function directory: {path!r}")
    if pure.parts and pure.parts[0] == ".":
        pure = PurePosixPath(*pure.parts[1:])
    if not pure.parts:
        raise ValueError(f"invalid path: {path!r}")
    return str(pure)


class _LogReader(threading.Thread):
    """Drains the function's standard error, keeping at most ``limit`` bytes."""

    def __init__(self, fd: int, limit: int):
        super().__init__(daemon=True)
        self._fd = fd
        self._limit = limit
        self._chunks: List[bytes] = []
        self._kept = 0
        self.truncated = False
        self._stop_event = threading.Event()

    def run(self) -> None:
        poller = select.poll()
        poller.register(self._fd, select.POLLIN)
        try:
            while not self._stop_event.is_set():
                if not poller.poll(100):
                    continue
                chunk = os.read(self._fd, 64 * 1024)
                if not chunk:
                    return
                room = self._limit - self._kept
                if room > 0:
                    self._chunks.append(chunk[:room])
                    self._kept += min(room, len(chunk))
                if len(chunk) > room:
                    self.truncated = True
        finally:
            os.close(self._fd)

    def finish(self, timeout: float) -> Tuple[str, bool]:
        self.join(timeout)
        if self.is_alive():
            # A descendant the runner lost still holds the pipe.
            self._stop_event.set()
            self.join(1.0)
        return b"".join(self._chunks).decode("utf-8", "replace"), self.truncated


def exit_from_report(
    final: Mapping[str, Any], *, cpu_limit_seconds: int, wall_ms: int
) -> RunExit:
    """The run's exit from the supervisor's final report."""
    stopped = bool(final.get("stopped"))
    exit_signal = final.get("signal")
    cpu_seconds = final.get("cpu_seconds") or 0
    outcome = "killed" if stopped else "exited"
    return RunExit(
        outcome,
        exit_code=final.get("exit_code"),
        signal=exit_signal,
        peak_memory_kib=final.get("peak_memory_kib"),
        wall_ms=wall_ms,
        detail=final.get("monitor_error", ""),
        disk_limit_reached=bool(final.get("disk_exceeded")),
        disk_check_failed=bool(final.get("monitor_error")),
        # Threads that release the interpreter lock can use CPU time faster
        # than wall-clock time; the kernel's backstop then ends the run before
        # the host's timer does.
        cpu_limit_reached=not stopped
        and exit_signal in (signal.SIGXCPU, signal.SIGKILL)
        and cpu_seconds >= cpu_limit_seconds,
    )


class RunHandle:
    """One started run: the function's standard streams and its supervisor."""

    def __init__(
        self,
        process: subprocess.Popen,
        *,
        stdin_fd: int,
        stdout_fd: int,
        status_fd: int,
        log_reader: _LogReader,
        run_root: str,
        started: float,
        cpu_limit_seconds: int,
    ):
        self._process = process
        self.stdin_fd = stdin_fd
        self.stdout_fd = stdout_fd
        self._status_fd = status_fd
        self._log_reader = log_reader
        self._run_root = run_root
        self._started = started
        self._cpu_limit_seconds = cpu_limit_seconds
        self._result: Optional[RunExit] = None

    def kill(self) -> None:
        """Ask the supervisor to terminate everything; idempotent."""
        if self._process.poll() is None:
            try:
                self._process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass

    def close_input(self) -> None:
        """Close the function's standard input; it then reads end-of-file."""
        if self.stdin_fd >= 0:
            os.close(self.stdin_fd)
            self.stdin_fd = -1

    def exited_within(self, timeout: float) -> bool:
        try:
            self._process.wait(timeout=max(timeout, 0))
            return True
        except subprocess.TimeoutExpired:
            return False

    def wait(self, timeout: float = STOP_TIMEOUT_SECONDS) -> RunExit:
        """Wait for the supervisor and report how the run's process ended."""
        if self._result is not None:
            return self._result
        lost_detail = ""
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            lost_detail = "The supervisor did not stop in time."
            self._process.kill()
            self._process.wait()
        wall_ms = int((time.monotonic() - self._started) * 1000)
        messages = self._read_status()
        final = next((m for m in messages if "exit_code" in m), None)
        setup = next((m for m in messages if "setup_error" in m), None)
        started = next((m for m in messages if "started" in m), None)

        if setup is not None:
            result = RunExit(
                "failed_to_start", wall_ms=wall_ms, detail=str(setup["setup_error"])
            )
        elif final is None or lost_detail:
            self._kill_leftovers(started)
            result = RunExit(
                "runner_lost",
                wall_ms=wall_ms,
                detail=lost_detail or "The supervisor ended without a report.",
            )
        else:
            result = exit_from_report(
                final, cpu_limit_seconds=self._cpu_limit_seconds, wall_ms=wall_ms
            )
        self.close_input()
        os.close(self.stdout_fd)
        result.log, result.log_truncated = self._log_reader.finish(2.0)
        shutil.rmtree(self._run_root, ignore_errors=True)
        self._result = result
        return result

    def _read_status(self) -> List[Dict[str, Any]]:
        data = b""
        try:
            os.set_blocking(self._status_fd, False)
            while True:
                try:
                    chunk = os.read(self._status_fd, 64 * 1024)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                data += chunk
        finally:
            os.close(self._status_fd)
        messages = []
        for line in data.splitlines():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                messages.append(message)
        return messages

    def _kill_leftovers(self, started: Optional[Dict[str, Any]]) -> None:
        # Best effort only: without the isolation control the function could
        # have killed its supervisor (§3.1).
        try:
            os.killpg(self._process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        pid = started.get("started") if started else None
        if isinstance(pid, int) and pid > 1:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


class HardenedSubprocessRunner:
    """The default runner of ADR 0012.

    ``use_landlock`` and ``use_seccomp`` exist so that tests and operators can
    exercise the degraded path; production uses everything the host offers.
    """

    def __init__(
        self,
        *,
        use_landlock: bool = True,
        use_seccomp: bool = True,
        max_disk_bytes: int = MAX_DISK_BYTES,
        max_disk_files: int = MAX_DISK_FILES,
    ):
        for name, value, maximum in (
            ("max_disk_bytes", max_disk_bytes, MAX_DISK_BYTES),
            ("max_disk_files", max_disk_files, MAX_DISK_FILES),
        ):
            if type(value) is not int or not 0 < value <= maximum:
                raise ValueError(
                    f"{name} must be an integer above 0 and at most {maximum}"
                )
        self._disk_limits = {"bytes": max_disk_bytes, "files": max_disk_files}
        self._use_landlock = use_landlock
        self._use_seccomp = use_seccomp
        self._python = os.path.realpath(sys.executable)
        self._probe: Optional[RunnerProbe] = None
        self._landlock_abi = 0
        self._seccomp = False
        self._runtime_paths: Tuple[str, ...] = ()
        self._list_paths: set = set()
        self._lock = threading.Lock()

    # -- probe -------------------------------------------------------------

    def probe(self) -> RunnerProbe:
        with self._lock:
            if self._probe is None:
                self._probe = self._run_probe()
            return self._probe

    def _run_probe(self) -> RunnerProbe:
        abi = sandbox.landlock_abi() if self._use_landlock else 0
        seccomp = self._use_seccomp and sandbox.syscall_table() is not None
        offers = [(abi, seccomp)]
        if abi and seccomp:
            offers += [(0, True), (abi, False)]
        offers.append((0, False))
        chosen = None
        for offer in dict.fromkeys(offers):
            if self._probe_offer(*offer):
                chosen = offer
                break
        if chosen is None:
            raise RunnerUnavailable("The host cannot start a confined process.")
        self._landlock_abi, self._seccomp = chosen
        self._runtime_paths = self._runtime_read_paths()
        held = _controls_for(*chosen)
        missing = tuple(c for c in CONTROLS if c not in held)
        level = LEVEL_FULL if not missing else LEVEL_DEGRADED
        return RunnerProbe(level=level, missing=missing, runtimes=(RUNTIME_PYTHON,))

    def _probe_offer(self, abi: int, seccomp: bool) -> bool:
        work = tempfile.mkdtemp(prefix="fnprobe-")
        try:
            spec = {
                "limits": self._limits(MAX_TIMEOUT_SECONDS, MAX_MEMORY_MIB),
                "landlock_abi": abi,
                "seccomp": seccomp,
                "read_paths": [],
                "work_dir": work,
            }
            messages = self._spawn_and_collect("--probe", spec)
            return any(m.get("probe_ok") is True for m in messages)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _spawn_and_collect(self, mode: str, spec: Dict[str, Any]) -> List[dict]:
        status_r, status_w = os.pipe()
        spec = dict(spec, status_fd=status_w, host_pid=os.getpid())
        try:
            process = subprocess.Popen(
                [self._python, "-I", "-S", _SANDBOX, mode, json.dumps(spec)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(status_w,),
                env={},
                start_new_session=True,
            )
        finally:
            os.close(status_w)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        with os.fdopen(status_r, "rb") as handle:
            lines = handle.read().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def _runtime_read_paths(self) -> Tuple[str, ...]:
        """What the Python runtime needs to read: the interpreter, its standard
        library and the shared libraries it loads, and nothing of the host."""
        paths = {self._python, "/etc/ld.so.cache"}
        for library_dir in _standard_library_dirs():
            # Third-party packages installed beside the standard library are
            # the application's, not the runtime's (§10): the directory itself
            # may only be listed, and everything in it but them may be read.
            self._list_paths.add(library_dir)
            for entry in os.listdir(library_dir):
                if entry not in _PACKAGE_DIRS:
                    paths.add(os.path.join(library_dir, entry))
        multiarch = sysconfig.get_config_var("MULTIARCH")
        for directory in ("/lib64", "/usr/lib64"):
            paths.add(directory)
        if multiarch:
            paths.update({f"/lib/{multiarch}", f"/usr/lib/{multiarch}"})
        for library in _mapped_libraries(self._python):
            paths.add(library)
        return tuple(sorted(p for p in paths if os.path.exists(p)))

    # -- start ---------------------------------------------------------------

    def start(self, spec: RunSpec) -> RunHandle:
        probe = self.probe()
        if spec.runtime not in probe.runtimes:
            raise ValueError(f"unsupported runtime: {spec.runtime!r}")
        entry = safe_relative_path(spec.entry_point)
        if entry not in {safe_relative_path(p) for p in spec.files}:
            raise ValueError("entry_point is not one of the function's files")

        run_root = tempfile.mkdtemp(prefix="fnrun-")
        try:
            fn_dir = os.path.join(run_root, "fn")
            work_dir = os.path.join(run_root, "work")
            os.mkdir(fn_dir)
            os.mkdir(work_dir)
            for relative, content in spec.files.items():
                target = os.path.join(fn_dir, safe_relative_path(relative))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as handle:
                    handle.write(content)
            argv = [
                self._python,
                "-I",
                "-S",
                "-B",
                "-X",
                "utf8",
                "-c",
                _PYTHON_BOOTSTRAP,
                fn_dir,
                HELPER_LIBRARY_DIR,
                os.path.join(fn_dir, entry),
            ]
            sandbox_spec = {
                "limits": self._limits(spec.timeout_seconds, spec.memory_mib),
                "landlock_abi": self._landlock_abi,
                "seccomp": self._seccomp,
                "read_paths": list(self._runtime_paths) + [HELPER_LIBRARY_DIR, fn_dir],
                "list_paths": sorted(self._list_paths),
                "work_dir": work_dir,
                "disk_limits": self._disk_limits,
                "argv": argv,
            }
            return self._spawn_run(spec, sandbox_spec, run_root)
        except BaseException:
            shutil.rmtree(run_root, ignore_errors=True)
            raise

    def _spawn_run(
        self, spec: RunSpec, sandbox_spec: Dict[str, Any], run_root: str
    ) -> RunHandle:
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        stderr_r, stderr_w = os.pipe()
        status_r, status_w = os.pipe()
        host_ends = [stdin_w, stdout_r, stderr_r, status_r]
        for fd in host_ends:
            os.set_inheritable(fd, False)
        sandbox_spec = dict(sandbox_spec, status_fd=status_w, host_pid=os.getpid())
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                [self._python, "-I", "-S", _SANDBOX, "--run", json.dumps(sandbox_spec)],
                stdin=stdin_r,
                stdout=stdout_w,
                stderr=stderr_w,
                pass_fds=(status_w,),
                env={},
                cwd=run_root,
                start_new_session=True,
            )
        except BaseException:
            for fd in host_ends:
                os.close(fd)
            raise
        finally:
            for fd in (stdin_r, stdout_w, stderr_w, status_w):
                os.close(fd)
        log_reader = _LogReader(stderr_r, spec.max_log_bytes)
        log_reader.start()
        return RunHandle(
            process,
            cpu_limit_seconds=sandbox_spec["limits"]["cpu_seconds"],
            stdin_fd=stdin_w,
            stdout_fd=stdout_r,
            status_fd=status_r,
            log_reader=log_reader,
            run_root=run_root,
            started=started,
        )

    @staticmethod
    def _limits(timeout_seconds: float, memory_mib: int) -> Dict[str, int]:
        return {
            "memory_mib": int(memory_mib),
            # A backstop under the host's wall-clock timer, never the limit.
            "cpu_seconds": int(timeout_seconds) + 2,
            "file_size_mib": FILE_SIZE_MIB,
        }


_PACKAGE_DIRS = frozenset({"site-packages", "dist-packages"})


def _standard_library_dirs() -> List[str]:
    """The base interpreter's standard library, not a virtual environment's."""
    base = {
        "base": sys.base_prefix,
        "platbase": sys.base_exec_prefix,
        "installed_base": sys.base_prefix,
        "installed_platbase": sys.base_exec_prefix,
    }
    paths = sysconfig.get_paths(vars=base)
    found = []
    for key in ("stdlib", "platstdlib"):
        value = paths.get(key)
        if value and os.path.isdir(value):
            found.append(os.path.realpath(value))
    return sorted(set(found))


def _mapped_libraries(python: str) -> List[str]:
    """Shared objects a bare interpreter maps at start-up."""
    try:
        completed = subprocess.run(
            [
                python,
                "-I",
                "-S",
                "-c",
                "print(open('/proc/self/maps').read())",
            ],
            capture_output=True,
            env={},
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    libraries = set()
    for line in completed.stdout.decode(errors="replace").splitlines():
        parts = line.split(None, 5)
        if len(parts) == 6 and parts[5].startswith("/") and ".so" in parts[5]:
            libraries.add(parts[5].strip())
    return sorted(libraries)


# --- one run, end to end ------------------------------------------------------


@dataclass
class FunctionRunResult:
    """A finished run in the terms of §11 (before output-schema validation)."""

    status: str
    code: Optional[str] = None
    message: str = ""
    output: Optional[Dict[str, Any]] = None
    function_error_code: str = ""
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    wall_ms: int = 0
    peak_memory_kib: Optional[int] = None
    log: str = ""
    log_truncated: bool = False
    retry_at: Optional[float] = None


def run_function(
    runner: HardenedSubprocessRunner,
    spec: RunSpec,
    broker: FunctionBroker,
    envelope: Dict[str, Any],
    *,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    runtime_extensions=None,
    attempt: int = 1,
) -> FunctionRunResult:
    """Execute one run: start it, serve its tool calls, terminate it."""
    extensions = runtime_extensions or get_runtime_extensions()
    with (
        use_request_authorization(
            **_authorization_kwargs(broker.context.authorization_inputs)
        ),
        bind_execution_context(None),
    ):
        context = runtime_context(
            "user_function",
            run_id=broker.context.run_id,
            correlation_id=broker.context.correlation_id,
            attempt=attempt,
        )
    try:
        with bind_runtime_extensions(extensions):
            extensions.require_admission(context)
    except AdmissionRefused as exc:
        return FunctionRunResult(
            "deferred" if exc.decision.outcome == "defer" else STATUS_FAILED,
            exc.code,
            str(exc),
            retry_at=exc.retry_at,
        )
    deadline = time.monotonic() + spec.timeout_seconds
    handle = runner.start(spec)
    outcome: Optional[SessionOutcome] = None
    try:
        with bind_execution_context(context), bind_runtime_extensions(extensions):
            outcome = run_session(
                broker,
                envelope,
                reader_fd=handle.stdout_fd,
                writer_fd=handle.stdin_fd,
                deadline=deadline,
                max_output_bytes=max_output_bytes,
            )
    finally:
        sent_result = outcome is not None and outcome.code not in (
            CODE_TIMEOUT,
            CODE_NO_RESULT,
            CODE_PROTOCOL_ERROR,
        )
        handle.close_input()
        if not (sent_result and handle.exited_within(EXIT_GRACE_SECONDS)):
            handle.kill()
        process_exit = handle.wait()
    return _result_for(outcome, process_exit, broker)


def _result_for(
    outcome: SessionOutcome, process_exit: RunExit, broker: FunctionBroker
) -> FunctionRunResult:
    common = dict(
        tool_calls=list(outcome.tool_calls or broker.tool_calls),
        wall_ms=process_exit.wall_ms,
        peak_memory_kib=process_exit.peak_memory_kib,
        log=process_exit.log,
        log_truncated=process_exit.log_truncated,
    )
    if process_exit.outcome in ("runner_lost", "failed_to_start"):
        # Nothing a run starts may outlive it; a run the runner lost control of
        # fails whatever the function reported.
        message = (
            "The runner could not start the function."
            if process_exit.outcome == "failed_to_start"
            else "The runner lost control of the run."
        )
        if process_exit.outcome == "failed_to_start":
            logger.warning("User function failed to start: %s", process_exit.detail)
        return FunctionRunResult(STATUS_FAILED, CODE_RUNNER_LOST, message, **common)
    if process_exit.disk_limit_reached:
        return FunctionRunResult(
            STATUS_FAILED,
            CODE_DISK_EXCEEDED,
            "The working-directory disk quota was exceeded.",
            **common,
        )
    if process_exit.disk_check_failed:
        return FunctionRunResult(
            STATUS_FAILED,
            CODE_DISK_CHECK_FAILED,
            "The working-directory disk quota could not be checked.",
            **common,
        )
    if outcome.code == CODE_TIMEOUT:
        return FunctionRunResult(
            STATUS_TIMED_OUT, CODE_TIMEOUT, outcome.error_message, **common
        )
    if outcome.code == CODE_NO_RESULT and process_exit.cpu_limit_reached:
        return FunctionRunResult(
            STATUS_TIMED_OUT, CODE_TIMEOUT, "The CPU-time limit was reached.", **common
        )
    if outcome.code is None:
        return FunctionRunResult(STATUS_SUCCEEDED, output=outcome.output, **common)
    return FunctionRunResult(
        STATUS_FAILED,
        outcome.code,
        outcome.error_message,
        function_error_code=outcome.function_error_code,
        **common,
    )
