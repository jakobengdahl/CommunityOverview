"""
Function registry, manual invocation and run records
(docs/USER_FUNCTION_CONTRACT.md §4.4, §5, §8, §11).

``FunctionService`` lists the functions under the configured root, accepts a
manual invocation as the calling actor, runs it in the background through the
runner and the broker, and records the run in the run-history store that agent
runs use. A run is executed at most once: its record is created RUNNING, is
never claimed, and a host crash leaves it to the retention sweep.

``invoke`` must be called inside the caller's request authorization context
(``backend.runtime.authorization.use_request_authorization``); the run keeps a
copy of it, so every tool call is evaluated for the invoking actor.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.agents.execution import (
    ExecutionJob,
    ExecutionKind,
    ExecutionState,
    ExecutionStore,
)
from backend.functions.broker import (
    GRANT_GRAPH_WRITE,
    BrokerContext,
    FunctionBroker,
)
from backend.functions.manifest import (
    LoadedFunction,
    is_valid_id,
    Manifest,
    list_function_directories,
    package_digest,
    read_package,
    validate_package,
)
from backend.functions.runner import (
    LEVEL_FULL,
    STATUS_SUCCEEDED,
    STATUS_TIMED_OUT,
    HardenedSubprocessRunner,
    RunHandle,
    RunSpec,
    run_function,
)
from backend.functions.schema_check import (
    SchemaCheckBusy,
    SchemaCheckFailed,
    depth_below,
    is_utf8,
    schema_problem,
)
from backend.functions.settings import FunctionsSettings
from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GRAPH_ACTION_READ,
    get_current_request_authorization_inputs,
)
from backend.service import access

logger = logging.getLogger(__name__)

TRIGGER_MANUAL = "manual"
MAX_INPUT_BYTES = 256 * 1024
MAX_OUTPUT_DEPTH = 64
MAX_KEY_CHARS = 200

STATUS_RUNNING = "running"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
RUN_STATUSES = (
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
    STATUS_CANCELLED,
    STATUS_REJECTED,
)

# Run-level codes this module produces (§11); the runner produces the rest.
CODE_INVALID_MANIFEST = "invalid_manifest"
CODE_NOT_ENABLED = "not_enabled"
CODE_ACCESS_DENIED = "access_denied"
CODE_INVALID_INPUT = "invalid_input"
CODE_BUSY = "busy"
CODE_ISOLATION_BELOW_MINIMUM = "isolation_below_minimum"
CODE_INVALID_OUTPUT = "invalid_output"
CODE_RUNNER_LOST = "runner_lost"


class FunctionNotFound(LookupError):
    pass


class InvocationError(ValueError):
    """The request itself is malformed; no run record is created."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


class _Rejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _ActiveRun:
    def __init__(self) -> None:
        self.handle: Optional[RunHandle] = None
        self.cancelled = False
        # Set once the process has ended and the outcome is being recorded:
        # a cancel from then on has nothing left to cancel.
        self.finalizing = False


class FunctionService:
    def __init__(
        self,
        graph_service,
        store: ExecutionStore,
        *,
        settings: FunctionsSettings,
        runner: Optional[HardenedSubprocessRunner] = None,
        read_cutoff: Callable[[], Optional[datetime]] = lambda: None,
    ):
        self._graph_service = graph_service
        self._store = store
        self._settings = settings
        self._runner = runner or HardenedSubprocessRunner()
        self._read_cutoff = read_cutoff
        self._lock = threading.Lock()
        self._admission_lock = threading.Lock()
        self._busy: set = set()
        self._active: Dict[str, _ActiveRun] = {}
        self._packages: Dict[str, Tuple[str, LoadedFunction]] = {}
        # Runs live only in this process; a record from another boot that
        # still reads "running" was cut short by a restart.
        self._boot_id = uuid.uuid4().hex

    def close_interrupted_runs(self) -> int:
        """Close runs a previous process left running (§11 ``runner_lost``)."""
        closed = 0
        for job in self._store.list_jobs(
            states=[ExecutionState.RUNNING], kind=ExecutionKind.FUNCTION
        ):
            if (job.payload or {}).get("boot_id") == self._boot_id:
                continue
            try:
                self._store.fail(
                    job.id,
                    error=f"{CODE_RUNNER_LOST}: The host restarted during the run.",
                    result={"outcome": STATUS_FAILED, "code": CODE_RUNNER_LOST},
                )
                closed += 1
            except Exception:  # noqa: BLE001 - one bad record must not stop the rest
                logger.exception("Could not close interrupted function run %s", job.id)
        if closed:
            logger.warning(
                "Closed %d user-function run(s) a restart interrupted", closed
            )
        return closed

    # -- the runner ---------------------------------------------------------------

    def runner_status(self) -> Dict[str, Any]:
        probe = self._runner.probe()
        return {
            "level": probe.level,
            "missing": list(probe.missing),
            "runtimes": list(probe.runtimes),
            "isolation_minimum": self._settings.isolation_minimum,
        }

    def log_runner_level(self) -> None:
        """Probe the runner once and warn when it runs below full isolation."""
        try:
            probe = self._runner.probe()
        except Exception:  # noqa: BLE001 - reported, never fatal at startup
            logger.exception("The user-function runner is unavailable")
            return
        if probe.level != LEVEL_FULL:
            logger.warning(
                "User functions run at isolation level %s; missing controls: %s. "
                "A function can read what the application user can read; set "
                "FUNCTIONS_ISOLATION_MINIMUM=full to refuse runs below full.",
                probe.level,
                ", ".join(probe.missing),
            )

    # -- the registry -------------------------------------------------------------

    def list_functions(self) -> List[Dict[str, Any]]:
        names = list_function_directories(self._settings.root)
        with self._lock:
            for gone in set(self._packages) - set(names):
                del self._packages[gone]
        return [self._function_view(self._read(name)) for name in names]

    def get_function(self, function_id: str) -> Dict[str, Any]:
        return self._function_view(self._load(function_id))

    def _load(self, function_id: str) -> LoadedFunction:
        # Only a directory named like a valid id can be invoked: run keys are
        # "fn:<id>:<key>" and must not collide across functions.
        if not is_valid_id(function_id) or function_id not in list_function_directories(
            self._settings.root
        ):
            raise FunctionNotFound(function_id)
        return self._read(function_id)

    def _read(self, directory_name: str) -> LoadedFunction:
        """A function's package as it is on disk now. Its files are read on
        every request; only validating the manifest is skipped when the
        content is unchanged, since large schemas would let readers spend the
        host's CPU."""
        read = read_package(self._settings.root, directory_name)
        if read.errors:
            return read
        digest = package_digest(read.files)
        with self._lock:
            cached = self._packages.get(directory_name)
        if cached is not None and cached[0] == digest:
            return cached[1]
        loaded = validate_package(read)
        with self._lock:
            self._packages[directory_name] = (digest, loaded)
        return loaded

    def _function_view(self, loaded: LoadedFunction) -> Dict[str, Any]:
        function_id = loaded.directory_name
        manifest = loaded.manifest
        declared = list(manifest.grants) if manifest else []
        return {
            "id": function_id,
            "valid": loaded.valid,
            "errors": list(loaded.errors),
            "enabled": self._settings.is_enabled(function_id),
            "grants": declared,
            "missing_grants": [
                g for g in declared if not self._settings.grant_enabled(function_id, g)
            ],
            "name": manifest.name if manifest else None,
            "description": manifest.description if manifest else None,
            "version": manifest.version if manifest else None,
            "runtime": manifest.runtime if manifest else None,
            "input_schema": manifest.input_schema if manifest else None,
        }

    # -- invocation ---------------------------------------------------------------

    def invoke(
        self,
        function_id: str,
        input_value: Any,
        *,
        idempotency_key: Optional[str] = None,
        correlation_id: Optional[str] = None,
        event_origin: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start a run, or record why it cannot start (§5, §11 ``rejected``)."""
        _check_text("idempotency_key", idempotency_key, allow_empty=False)
        _check_text("correlation_id", correlation_id)
        _check_text("event_origin", event_origin)
        if not is_valid_id(function_id):
            raise FunctionNotFound(function_id)
        key = f"fn:{function_id}:{idempotency_key or uuid.uuid4().hex}"
        if idempotency_key is not None:
            # A repeated key returns the earlier run in any state (§5.3), even
            # when the function or the runner is gone since.
            existing = self._store.get_by_key(key)
            if existing is not None:
                return function_run_to_dict(existing)
        loaded = self._load(function_id)
        correlation_id = correlation_id or f"corr-{uuid.uuid4().hex}"
        authorization_inputs = get_current_request_authorization_inputs()
        attribution = access.build_mutation_attribution(target="functions.invoke")
        actor = attribution.to_dict() if attribution is not None else None
        manifest = loaded.manifest
        recorded = dict(correlation_id=correlation_id, origin=event_origin, actor=actor)

        try:
            probe, limits = self._admit(loaded, input_value)
        except _Rejected as rejected:
            return self._record_rejection(
                key, function_id, manifest, rejected, **recorded
            )

        job = self._new_job(
            key,
            function_id,
            manifest,
            state=ExecutionState.RUNNING,
            isolation={"level": probe.level, "missing": list(probe.missing)},
            **recorded,
        )
        envelope = self._envelope(
            job.id,
            manifest,
            input_value,
            correlation_id=correlation_id,
            event_origin=event_origin,
            actor=actor,
            idempotency_key=idempotency_key,
            timeout_seconds=limits["timeout_seconds"],
        )
        context = BrokerContext(
            run_id=job.id,
            function_id=function_id,
            function_version=manifest.version,
            declared_grants=frozenset(manifest.grants),
            correlation_id=correlation_id,
            authorization_inputs=authorization_inputs,
            grant_enabled=lambda grant: self._settings.grant_enabled(
                function_id, grant
            ),
            max_tool_calls=limits["max_tool_calls"],
        )
        spec = RunSpec(
            run_id=job.id,
            runtime=manifest.runtime,
            entry_point=manifest.entry_point,
            files=dict(loaded.files),
            timeout_seconds=limits["timeout_seconds"],
            memory_mib=limits["memory_mib"],
        )
        thread = threading.Thread(
            target=self._execute,
            args=(job.id, function_id, manifest, spec, context, envelope),
            name=f"function-run-{job.id}",
            daemon=True,
        )

        # The busy check and the record are one step, so that an invocation
        # repeating a key that is being recorded finds that run, not "busy".
        with self._admission_lock:
            with self._lock:
                busy = function_id in self._busy
            if busy:
                return self._record_rejection(
                    key,
                    function_id,
                    manifest,
                    _Rejected(
                        CODE_BUSY, "Another run of this function is in progress."
                    ),
                    **recorded,
                )
            stored = self._store.enqueue(job)
            if stored.id != job.id:
                return function_run_to_dict(stored)
            with self._lock:
                self._busy.add(function_id)
                self._active[job.id] = _ActiveRun()
        try:
            thread.start()
        except BaseException:
            self._finish_lost(
                job.id, function_id, "The runner could not start the run."
            )
            raise
        return function_run_to_dict(stored)

    def _finish_lost(self, run_id: str, function_id: str, message: str) -> None:
        """Close a run the host could not carry through; never leave it running."""
        try:
            self._store.fail(
                run_id,
                error=f"{CODE_RUNNER_LOST}: {message}",
                result={"outcome": STATUS_FAILED, "code": CODE_RUNNER_LOST},
            )
        except Exception:  # noqa: BLE001 - the retention sweep closes it then
            logger.exception("Could not record user-function run %s", run_id)
        finally:
            with self._lock:
                self._active.pop(run_id, None)
            self._release(function_id)

    def _admit(self, loaded: LoadedFunction, input_value: Any):
        """The §11 ``rejected`` checks, in order; raises _Rejected."""
        hook = self._graph_service.authorization_hook
        self._require_access(hook, GRAPH_ACTION_READ)
        if not loaded.valid:
            raise _Rejected(
                CODE_INVALID_MANIFEST,
                "The manifest is invalid: " + "; ".join(loaded.errors),
            )
        manifest: Manifest = loaded.manifest
        probe = self._runner.probe()
        if manifest.runtime not in probe.runtimes:
            raise _Rejected(
                CODE_INVALID_MANIFEST,
                f"The {manifest.runtime} runtime is not available on this host.",
            )
        if not self._settings.is_enabled(manifest.id) or any(
            not self._settings.grant_enabled(manifest.id, g) for g in manifest.grants
        ):
            raise _Rejected(
                CODE_NOT_ENABLED, "The function or one of its grants is not enabled."
            )
        if GRANT_GRAPH_WRITE in manifest.grants:
            self._require_access(hook, GRAPH_ACTION_MUTATE)
        # Checked again under the admission lock; this early look only spares
        # the input check for a run that could not start anyway.
        if manifest.id in self._busy:
            raise _Rejected(CODE_BUSY, "Another run of this function is in progress.")
        _validate_input(manifest, input_value)
        if self._settings.isolation_minimum == LEVEL_FULL and probe.level != LEVEL_FULL:
            raise _Rejected(
                CODE_ISOLATION_BELOW_MINIMUM,
                "The runner's isolation level is below the instance minimum.",
            )
        limits = {
            "timeout_seconds": min(
                manifest.limits.get("timeout_seconds", self._settings.timeout_seconds),
                self._settings.timeout_seconds,
            ),
            "memory_mib": min(
                manifest.limits.get("memory_mib", self._settings.memory_mib),
                self._settings.memory_mib,
            ),
            "max_tool_calls": min(
                manifest.limits.get("max_tool_calls", self._settings.max_tool_calls),
                self._settings.max_tool_calls,
            ),
        }
        return probe, limits

    @staticmethod
    def _require_access(hook, action: str) -> None:
        try:
            decision = access.evaluate_graph_access(
                hook, action=action, target="functions.invoke"
            )
        except Exception:  # noqa: BLE001 - a failing hook denies
            logger.exception("Authorization hook failed for a function invocation")
            decision = None
        if decision is None or not decision.allowed:
            raise _Rejected(
                CODE_ACCESS_DENIED,
                "The caller may not "
                + ("read" if action == GRAPH_ACTION_READ else "change")
                + " this graph.",
            )

    def _release(self, function_id: str) -> None:
        with self._lock:
            self._busy.discard(function_id)

    # -- records ------------------------------------------------------------------

    def _new_job(
        self,
        key: str,
        function_id: str,
        manifest: Optional[Manifest],
        *,
        state: ExecutionState,
        correlation_id: str,
        origin: Optional[str],
        actor: Optional[Dict[str, Any]],
        isolation: Optional[Dict[str, Any]] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> ExecutionJob:
        now = _now()
        job = ExecutionJob(
            agent_id=f"function:{function_id}",
            kind=ExecutionKind.FUNCTION,
            idempotency_key=key,
            payload={
                "function_id": function_id,
                "function_version": manifest.version if manifest else None,
                "trigger": TRIGGER_MANUAL,
                "actor": actor,
                "isolation": isolation,
                "boot_id": self._boot_id,
            },
            state=state,
            attempts=1 if state == ExecutionState.RUNNING else 0,
            run_at=now,
            correlation_id=correlation_id,
            origin=origin,
            result=result,
            created_at=now,
        )
        if state == ExecutionState.RUNNING:
            job.started_at = now
        else:
            job.finished_at = now
        return job

    def _record_rejection(
        self,
        key: str,
        function_id: str,
        manifest: Optional[Manifest],
        rejected: _Rejected,
        **fields: Any,
    ) -> Dict[str, Any]:
        job = self._new_job(
            key,
            function_id,
            manifest,
            state=ExecutionState.CANCELLED,
            result={
                "outcome": STATUS_REJECTED,
                "code": rejected.code,
                "message": _scrub(rejected.message)[:1000],
            },
            **fields,
        )
        # A repeated key returns the earlier run, whatever its state (§5.3).
        return function_run_to_dict(self._store.enqueue(job))

    # -- the run ------------------------------------------------------------------

    def _execute(
        self,
        run_id: str,
        function_id: str,
        manifest: Manifest,
        spec: RunSpec,
        context: BrokerContext,
        envelope: Dict[str, Any],
    ) -> None:
        active = self._active[run_id]
        try:
            result = run_function(
                self._runner,
                spec,
                FunctionBroker(self._graph_service, context),
                envelope,
                on_start=lambda handle: self._started(active, handle),
            )
        except Exception:  # noqa: BLE001 - recorded as a lost run
            logger.exception("User-function run %s could not be executed", run_id)
            self._finish_lost(
                run_id, function_id, "The runner could not run the function."
            )
            return
        with self._lock:
            active.finalizing = True
            cancelled = active.cancelled
        try:
            self._record_outcome(run_id, manifest, result, cancelled)
        except Exception:  # noqa: BLE001 - never leave the record running
            logger.exception("Recording user-function run %s failed", run_id)
            self._finish_lost(
                run_id, function_id, "The run's outcome could not be recorded."
            )
            return
        with self._lock:
            self._active.pop(run_id, None)
        self._release(function_id)

    def _record_outcome(
        self, run_id: str, manifest: Manifest, result, cancelled: bool
    ) -> None:
        details = {
            "output": None,
            "function_error_code": _scrub(result.function_error_code) or None,
            "tool_calls": [
                {
                    "tool": _scrub(c.tool),
                    "outcome": _scrub(c.outcome),
                    "duration_ms": c.duration_ms,
                }
                for c in result.tool_calls
            ],
            "usage": {
                "wall_ms": result.wall_ms,
                "peak_memory_kib": result.peak_memory_kib,
            },
            "log": _scrub(result.log),
            "log_truncated": result.log_truncated,
        }
        status, code, message = result.status, result.code, _scrub(result.message)
        if status == STATUS_SUCCEEDED and not depth_below(
            result.output, MAX_OUTPUT_DEPTH
        ):
            # Records are read back on request threads; a deeper value could
            # make every listing that includes it fail.
            status, code = STATUS_FAILED, CODE_INVALID_OUTPUT
            message = f"result.output nests deeper than {MAX_OUTPUT_DEPTH} levels"
        elif status == STATUS_SUCCEEDED and not is_utf8(result.output):
            # JSON can carry lone surrogates that no UTF-8 response can.
            status, code = STATUS_FAILED, CODE_INVALID_OUTPUT
            message = "result.output holds text that is not valid Unicode"
        elif status == STATUS_SUCCEEDED and manifest.output_schema is not None:
            try:
                problem = schema_problem(
                    manifest.output_schema, result.output, output=True
                )
            except SchemaCheckFailed as exc:
                problem = f"output_schema could not be applied ({exc})"
            if problem:
                status, code = STATUS_FAILED, CODE_INVALID_OUTPUT
                message = f"result.output failed output_schema: {problem}"
        if cancelled and status != STATUS_SUCCEEDED:
            # Only a run the cancel actually stopped is cancelled; one that had
            # already finished keeps its outcome.
            self._store.cancel(
                run_id,
                result=dict(details, outcome=STATUS_CANCELLED, code=None, message=""),
            )
        elif status == STATUS_SUCCEEDED:
            details["output"] = result.output
            self._store.complete(run_id, result=dict(details, outcome=STATUS_SUCCEEDED))
        else:
            self._store.fail(
                run_id,
                error=f"{code}: {message}"[:1100],
                result=dict(details, outcome=status, code=code, message=message),
            )

    def _started(self, active: _ActiveRun, handle: RunHandle) -> None:
        with self._lock:
            active.handle = handle
            cancelled = active.cancelled
        if cancelled:
            handle.kill()

    def cancel_run(self, run_id: str) -> bool:
        """Terminate a run in progress (§11 ``cancelled``); False if not running."""
        with self._lock:
            active = self._active.get(run_id)
            if active is None or active.finalizing:
                return False
            active.cancelled = True
            handle = active.handle
        if handle is not None:
            handle.kill()
        return True

    # -- reading records ------------------------------------------------------------

    def list_runs(
        self,
        *,
        function_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        viewer: Optional["RunViewer"] = None,
    ) -> List[Dict[str, Any]]:
        if status is not None and status not in RUN_STATUSES:
            return []
        agent_id = f"function:{function_id}" if function_id else None
        states = [_STATUS_STATES[status]] if status else None
        exact = status is None or status in _STATUSES_WITH_OWN_STATE
        # Two statuses share one store state; a bounded look-back keeps one
        # request from loading the whole history.
        jobs = self._store.list_jobs(
            kind=ExecutionKind.FUNCTION,
            agent_id=agent_id,
            states=states,
            limit=limit if exact else limit * _SHARED_STATE_LOOKBACK,
            created_after=self._read_cutoff(),
        )
        runs = [function_run_to_dict(job) for job in jobs]
        if status is not None:
            runs = [run for run in runs if run["status"] == status][:limit]
        # A listing summarizes; output and log are read one run at a time.
        return [dict(_for_viewer(run, viewer), output=None, log=None) for run in runs]

    def get_run(
        self, run_id: str, *, viewer: Optional["RunViewer"] = None
    ) -> Optional[Dict[str, Any]]:
        job = self._store.get(run_id)
        cutoff = self._read_cutoff()
        if (
            job is None
            or job.kind != ExecutionKind.FUNCTION
            or (cutoff is not None and job.created_at <= cutoff)
        ):
            return None
        return _for_viewer(function_run_to_dict(job), viewer)

    # -- the envelope ---------------------------------------------------------------

    def _envelope(
        self,
        run_id: str,
        manifest: Manifest,
        input_value: Dict[str, Any],
        *,
        correlation_id: str,
        event_origin: Optional[str],
        actor: Optional[Dict[str, Any]],
        idempotency_key: Optional[str],
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        now = _now()
        return {
            "schema_version": "1.0",
            "event_id": f"evt-{uuid.uuid4().hex}",
            "event_type": "function.invoke",
            "occurred_at": _iso(now),
            "origin": {
                "event_origin": event_origin,
                "event_session_id": None,
                "event_correlation_id": correlation_id,
                "attribution": actor,
            },
            "entity": None,
            "invocation": {
                "contract_version": manifest.contract_version,
                "run_id": run_id,
                "function_id": manifest.id,
                "function_version": manifest.version,
                "trigger": TRIGGER_MANUAL,
                "input": input_value,
                "idempotency_key": idempotency_key,
                "chain_depth": 0,
                "deadline": _iso(now + timedelta(seconds=timeout_seconds)),
            },
        }


# The store state each run status is recorded in (§11); two pairs share one.
_STATUS_STATES = {
    STATUS_RUNNING: ExecutionState.RUNNING,
    STATUS_SUCCEEDED: ExecutionState.SUCCEEDED,
    STATUS_FAILED: ExecutionState.DEAD_LETTER,
    STATUS_TIMED_OUT: ExecutionState.DEAD_LETTER,
    STATUS_CANCELLED: ExecutionState.CANCELLED,
    STATUS_REJECTED: ExecutionState.CANCELLED,
}
_STATUSES_WITH_OWN_STATE = frozenset({STATUS_RUNNING, STATUS_SUCCEEDED})
_SHARED_STATE_LOOKBACK = 4


@dataclass(frozen=True)
class RunViewer:
    """Who reads run records: a reader whose graph visibility is narrowed
    sees the content of only the runs they started themselves."""

    actor_id: str = ""
    narrowed: bool = False


# Everything a function or its caller controls the text of: a narrowed reader
# of someone else's run keeps its status, error code, timing and tool names.
_CONTENT_FIELDS = (
    "output",
    "log",
    "actor",
    "function_error_code",
    "correlation_id",
    "origin",
)


def _for_viewer(run: Dict[str, Any], viewer: Optional[RunViewer]) -> Dict[str, Any]:
    if viewer is None or not viewer.narrowed:
        return run
    started_by = ((run.get("actor") or {}).get("actor") or {}).get("actor_id")
    if viewer.actor_id and started_by == viewer.actor_id:
        return run
    hidden = dict(run)
    for name in _CONTENT_FIELDS:
        hidden[name] = None
    if hidden.get("error"):
        hidden["error"] = {"code": hidden["error"].get("code"), "message": ""}
    hidden["content_hidden"] = True
    return hidden


def view_run(run: Dict[str, Any], viewer: Optional["RunViewer"]) -> Dict[str, Any]:
    """A run record as ``viewer`` may see it."""
    return _for_viewer(run, viewer)


def _check_text(name: str, value: Any, *, allow_empty: bool = True) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or len(value) > MAX_KEY_CHARS
        or (not allow_empty and not value)
    ):
        raise InvocationError(
            f"{name} must be a string of at most {MAX_KEY_CHARS} characters"
        )
    if not is_utf8(value):
        raise InvocationError(f"{name} must be valid Unicode text")


def _scrub(text: Any) -> Any:
    """Text that can be stored and served as UTF-8, lone surrogates replaced."""
    if not isinstance(text, str):
        return text
    return text.encode("utf-8", "replace").decode("utf-8")


def _validate_input(manifest: Manifest, input_value: Any) -> None:
    if not isinstance(input_value, dict):
        raise _Rejected(CODE_INVALID_INPUT, "input must be a JSON object.")
    try:
        size = len(json.dumps(input_value).encode("utf-8"))
    except (TypeError, ValueError):
        raise _Rejected(CODE_INVALID_INPUT, "input must be JSON.") from None
    if size > MAX_INPUT_BYTES:
        raise _Rejected(
            CODE_INVALID_INPUT, f"input is larger than {MAX_INPUT_BYTES} bytes."
        )
    if manifest.input_schema is None:
        if input_value:
            raise _Rejected(
                CODE_INVALID_INPUT,
                "This function takes no input: send an empty object.",
            )
        return
    try:
        problem = schema_problem(manifest.input_schema, input_value)
    except SchemaCheckBusy:
        raise
    except SchemaCheckFailed as exc:
        raise _Rejected(
            CODE_INVALID_MANIFEST, f"input_schema could not be applied ({exc})."
        ) from None
    if problem:
        raise _Rejected(CODE_INVALID_INPUT, f"input failed input_schema: {problem}")


RUN_CODES = frozenset(
    {
        CODE_INVALID_MANIFEST,
        CODE_NOT_ENABLED,
        CODE_ACCESS_DENIED,
        CODE_INVALID_INPUT,
        "chain_depth_exceeded",
        CODE_BUSY,
        CODE_ISOLATION_BELOW_MINIMUM,
        "function_error",
        CODE_RUNNER_LOST,
        "no_result",
        "protocol_error",
        "memory_exceeded",
        "output_too_large",
        CODE_INVALID_OUTPUT,
        "timeout",
    }
)


def _status_and_error(job: ExecutionJob) -> Tuple[str, Optional[Dict[str, str]]]:
    result = job.result or {}
    if job.state == ExecutionState.SUCCEEDED:
        return STATUS_SUCCEEDED, None
    if job.state == ExecutionState.DEAD_LETTER:
        prefix, _, message = (job.last_error or "").partition(": ")
        code = result.get("code") or prefix
        if code not in RUN_CODES:
            # A record the host never closed itself, e.g. abandoned by the
            # retention sweep after a crash: the runner lost control of it.
            code, message = CODE_RUNNER_LOST, job.last_error or ""
        status = STATUS_TIMED_OUT if code == "timeout" else STATUS_FAILED
        return status, {"code": code, "message": message}
    if job.state == ExecutionState.CANCELLED:
        if result.get("outcome") == STATUS_REJECTED:
            return STATUS_REJECTED, {
                "code": result.get("code") or "",
                "message": result.get("message") or "",
            }
        return STATUS_CANCELLED, None
    return STATUS_RUNNING, None


def function_run_to_dict(job: ExecutionJob) -> Dict[str, Any]:
    """The run record of §11 for a stored job."""
    payload = job.payload or {}
    result = job.result or {}
    status, error = _status_and_error(job)
    tool_calls = result.get("tool_calls") or []
    return {
        "id": job.id,
        "function_id": payload.get("function_id")
        or job.agent_id.removeprefix("function:"),
        "function_version": payload.get("function_version"),
        "trigger": payload.get("trigger", TRIGGER_MANUAL),
        "status": status,
        "error": error,
        "function_error_code": result.get("function_error_code"),
        "output": result.get("output") if status == STATUS_SUCCEEDED else None,
        "correlation_id": job.correlation_id,
        "origin": job.origin,
        "actor": payload.get("actor"),
        "tool_calls": {"count": len(tool_calls), "calls": tool_calls},
        "usage": result.get("usage"),
        "isolation": payload.get("isolation"),
        "log": result.get("log"),
        "log_truncated": bool(result.get("log_truncated")),
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "redacted": job.redacted_at is not None,
        "content_hidden": False,
    }
