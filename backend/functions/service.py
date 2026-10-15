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
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from jsonschema import Draft202012Validator

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
    Manifest,
    list_function_directories,
    load_function,
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
        self._busy: set = set()
        self._active: Dict[str, _ActiveRun] = {}

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
        return [
            self._function_view(load_function(self._settings.root, name))
            for name in list_function_directories(self._settings.root)
        ]

    def get_function(self, function_id: str) -> Dict[str, Any]:
        return self._function_view(self._load(function_id))

    def _load(self, function_id: str) -> LoadedFunction:
        if function_id not in list_function_directories(self._settings.root):
            raise FunctionNotFound(function_id)
        return load_function(self._settings.root, function_id)

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
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or not 0 < len(idempotency_key) <= MAX_KEY_CHARS
        ):
            raise InvocationError(
                f"idempotency_key must be a string of 1-{MAX_KEY_CHARS} characters"
            )
        if correlation_id is not None and (
            not isinstance(correlation_id, str) or len(correlation_id) > MAX_KEY_CHARS
        ):
            raise InvocationError("correlation_id is too long")
        loaded = self._load(function_id)
        key = f"fn:{function_id}:{idempotency_key or uuid.uuid4().hex}"
        correlation_id = correlation_id or f"corr-{uuid.uuid4().hex}"
        authorization_inputs = get_current_request_authorization_inputs()
        attribution = access.build_mutation_attribution(target="functions.invoke")
        actor = attribution.to_dict() if attribution is not None else None
        manifest = loaded.manifest

        try:
            probe, limits = self._admit(loaded, input_value)
        except _Rejected as rejected:
            return self._record_rejection(
                key,
                function_id,
                manifest,
                rejected,
                correlation_id=correlation_id,
                origin=event_origin,
                actor=actor,
            )

        with self._lock:
            busy = function_id in self._busy
            if not busy:
                self._busy.add(function_id)
        if busy:
            return self._record_rejection(
                key,
                function_id,
                manifest,
                _Rejected(CODE_BUSY, "Another run of this function is in progress."),
                correlation_id=correlation_id,
                origin=event_origin,
                actor=actor,
            )

        try:
            job = self._new_job(
                key,
                function_id,
                manifest,
                state=ExecutionState.RUNNING,
                correlation_id=correlation_id,
                origin=event_origin,
                actor=actor,
                isolation={"level": probe.level, "missing": list(probe.missing)},
            )
            stored = self._store.enqueue(job)
            if stored.id != job.id:
                # The key names an earlier run: return it, start nothing (§5.3).
                self._release(function_id)
                return function_run_to_dict(stored)
            envelope = self._envelope(
                stored.id,
                manifest,
                input_value,
                correlation_id=correlation_id,
                event_origin=event_origin,
                actor=actor,
                idempotency_key=idempotency_key,
                timeout_seconds=limits["timeout_seconds"],
            )
            context = BrokerContext(
                run_id=stored.id,
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
                run_id=stored.id,
                runtime=manifest.runtime,
                entry_point=manifest.entry_point,
                files=dict(loaded.files),
                timeout_seconds=limits["timeout_seconds"],
                memory_mib=limits["memory_mib"],
            )
            with self._lock:
                self._active[stored.id] = _ActiveRun()
            thread = threading.Thread(
                target=self._execute,
                args=(stored.id, function_id, manifest, spec, context, envelope),
                name=f"function-run-{stored.id}",
                daemon=True,
            )
            thread.start()
        except BaseException:
            self._release(function_id)
            raise
        return function_run_to_dict(stored)

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
                "message": rejected.message[:1000],
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
                self._store.fail(
                    run_id,
                    error=f"{CODE_RUNNER_LOST}: The runner could not run the function.",
                    result={"outcome": STATUS_FAILED, "code": CODE_RUNNER_LOST},
                )
                return
            details = {
                "output": None,
                "function_error_code": result.function_error_code or None,
                "tool_calls": [
                    {"tool": c.tool, "outcome": c.outcome, "duration_ms": c.duration_ms}
                    for c in result.tool_calls
                ],
                "usage": {
                    "wall_ms": result.wall_ms,
                    "peak_memory_kib": result.peak_memory_kib,
                },
                "log": result.log,
                "log_truncated": result.log_truncated,
            }
            status, code, message = result.status, result.code, result.message
            if status == STATUS_SUCCEEDED and manifest.output_schema is not None:
                problem = _schema_problem(manifest.output_schema, result.output)
                if problem:
                    status, code = STATUS_FAILED, CODE_INVALID_OUTPUT
                    message = f"result.output failed output_schema: {problem}"
            if active.cancelled:
                self._store.cancel(
                    run_id,
                    result=dict(
                        details, outcome=STATUS_CANCELLED, code=None, message=""
                    ),
                )
            elif status == STATUS_SUCCEEDED:
                details["output"] = result.output
                self._store.complete(
                    run_id, result=dict(details, outcome=STATUS_SUCCEEDED)
                )
            else:
                self._store.fail(
                    run_id,
                    error=f"{code}: {message}"[:1100],
                    result=dict(details, outcome=status, code=code, message=message),
                )
        except Exception:  # noqa: BLE001 - recording must never kill the thread silently
            logger.exception("Recording user-function run %s failed", run_id)
        finally:
            with self._lock:
                self._active.pop(run_id, None)
            self._release(function_id)

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
            if active is None:
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
    ) -> List[Dict[str, Any]]:
        if status is not None and status not in RUN_STATUSES:
            return []
        agent_id = f"function:{function_id}" if function_id else None
        jobs = self._store.list_jobs(
            kind=ExecutionKind.FUNCTION,
            agent_id=agent_id,
            limit=None if status else limit,
            created_after=self._read_cutoff(),
        )
        runs = [function_run_to_dict(job) for job in jobs]
        if status is not None:
            runs = [run for run in runs if run["status"] == status][:limit]
        return runs

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        job = self._store.get(run_id)
        cutoff = self._read_cutoff()
        if (
            job is None
            or job.kind != ExecutionKind.FUNCTION
            or (cutoff is not None and job.created_at <= cutoff)
        ):
            return None
        return function_run_to_dict(job)

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


def _schema_problem(schema: Dict[str, Any], value: Any) -> Optional[str]:
    error = next(iter(Draft202012Validator(schema).iter_errors(value)), None)
    return error.message[:500] if error is not None else None


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
    problem = _schema_problem(manifest.input_schema, input_value)
    if problem:
        raise _Rejected(CODE_INVALID_INPUT, f"input failed input_schema: {problem}")


def _status_and_error(job: ExecutionJob) -> Tuple[str, Optional[Dict[str, str]]]:
    result = job.result or {}
    if job.state == ExecutionState.RUNNING:
        return STATUS_RUNNING, None
    if job.state == ExecutionState.SUCCEEDED:
        return STATUS_SUCCEEDED, None
    if job.state == ExecutionState.DEAD_LETTER:
        code, _, message = (job.last_error or "").partition(": ")
        status = STATUS_TIMED_OUT if code == "timeout" else STATUS_FAILED
        return status, {"code": code, "message": message}
    if job.state == ExecutionState.CANCELLED:
        if result.get("outcome") == STATUS_REJECTED:
            return STATUS_REJECTED, {
                "code": result.get("code", ""),
                "message": result.get("message", ""),
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
        "function_id": payload.get("function_id"),
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
    }
