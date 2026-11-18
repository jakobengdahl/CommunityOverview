"""Manifest admission and durable, at-most-once user-function invocation."""

from __future__ import annotations

import json
import fcntl
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

from jsonschema import validators
from jsonschema.exceptions import ValidationError

from backend.agents.execution import (
    ExecutionJob,
    ExecutionKind,
    ExecutionState,
    SqliteExecutionStore,
    utcnow,
)
from backend.functions.broker import BrokerContext, FunctionBroker, KNOWN_GRANTS
from backend.functions.runner import (
    HardenedSubprocessRunner,
    RunSpec,
    RunnerUnavailable,
    run_function,
    safe_relative_path,
)
from backend.runtime.authorization import use_request_authorization
from backend.service.access import evaluate_graph_access

# The local host uses one process with threads. Share admission across service
# instances too; the durable RUNNING records also block runs after a crash.
_ADMISSION_LOCK = threading.RLock()
logger = logging.getLogger(__name__)


@contextmanager
def _admission(root):
    # A directory lock serializes admission in local processes sharing packages,
    # without creating lock files. It is released as soon as history is durable.
    with _ADMISSION_LOCK:
        descriptor = None
        try:
            try:
                descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            except FileNotFoundError:
                pass  # No package can run until the configured root exists.
            if descriptor is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)


RunRecord = ExecutionJob
_ID = re.compile(r"[a-z0-9-]{3,64}\Z")
_VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
_LIMITS = {"timeout_seconds": 120, "memory_mib": 256, "max_tool_calls": 200}
_FAILURE_CODES = frozenset(
    {
        "function_error",
        "runner_lost",
        "no_result",
        "protocol_error",
        "memory_exceeded",
        "disk_exceeded",
        "disk_check_failed",
        "output_too_large",
        "invalid_output",
        "timeout",
    }
)


def _schema(schema):
    validator = validators.validator_for(schema)
    validator.check_schema(schema)

    # Validation must never retrieve a schema over the network or filesystem.
    def check(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                    not isinstance(item, str) or not item.startswith("#")
                ):
                    raise ValueError("External schema references are unsupported")
                if key == "$id" and isinstance(item, str) and not item.startswith("#"):
                    raise ValueError("External schema identifiers are unsupported")
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    check(schema)
    return validator(schema)


def _valid_triggers(manifest):
    triggers = manifest.get("triggers")
    if manifest.get("contract_version") == "1":
        return triggers == [{"kind": "manual"}]
    return (
        isinstance(triggers, list)
        and bool(triggers)
        and all(item in ({"kind": "manual"}, {"kind": "webhook"}) for item in triggers)
        and len({item["kind"] for item in triggers}) == len(triggers)
    )


def load_manifest(root, function_id):
    """Read and validate a package without following paths outside its directory."""
    if not isinstance(function_id, str) or not _ID.fullmatch(function_id):
        raise ValueError("Invalid function id")
    root_real = os.path.realpath(root)
    root_prefix = root_real.rstrip(os.sep)
    root = Path(root_real)
    directory_real = os.path.realpath(os.path.join(root_real, function_id))
    directory = Path(directory_real)
    if (
        not directory_real.startswith(root_prefix + os.sep)
        or not directory.is_relative_to(root)
        or directory.parent != root
        or directory.name != function_id
    ):
        raise ValueError("Invalid function directory")
    manifest_real = os.path.realpath(os.path.join(directory_real, "function.json"))
    manifest_path = Path(manifest_real)
    if not manifest_real.startswith(
        directory_real + os.sep
    ) or not manifest_path.is_relative_to(directory):
        raise ValueError("Invalid manifest path")
    manifest = json.loads(Path(manifest_real).read_text())
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be an object")
    for key in ("contract_version", "id", "version", "name", "runtime", "entry_point"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise ValueError("Missing or invalid required field")
    prerelease = manifest["version"].split("+", 1)[0].partition("-")[2]
    if any(
        part.isdigit() and len(part) > 1 and part.startswith("0")
        for part in prerelease.split(".")
    ):
        raise ValueError("Invalid semantic version")
    if (
        manifest["contract_version"] not in {"1", "2"}
        or manifest["id"] != function_id
        or not _VERSION.fullmatch(manifest["version"])
        or manifest["runtime"] != "python"
        or not _valid_triggers(manifest)
        or manifest.get("write_target", "graph") != "graph"
        or any(key in manifest for key in ("retry", "secrets", "egress"))
        or ("description" in manifest and not isinstance(manifest["description"], str))
    ):
        raise ValueError("Unsupported manifest")
    grants = manifest.get("grants")
    if not isinstance(grants, list) or any(
        not isinstance(grant, str) or grant not in KNOWN_GRANTS for grant in grants
    ):
        raise ValueError("Invalid grants")
    limits = manifest.get("limits", {})
    if not isinstance(limits, dict) or any(
        key not in _LIMITS or type(value) is not int or not 0 < value <= _LIMITS[key]
        for key, value in limits.items()
    ):
        raise ValueError("Invalid limits")
    for key in ("input_schema", "output_schema"):
        if key in manifest:
            _schema(manifest[key])
    entry_real = os.path.realpath(
        os.path.join(directory_real, safe_relative_path(manifest["entry_point"]))
    )
    entry = Path(entry_real)
    if (
        not entry_real.startswith(directory_real + os.sep)
        or not entry.is_relative_to(directory)
        or not Path(entry_real).is_file()
    ):
        raise ValueError("Invalid entry point")
    files = {}
    if not directory_real.startswith(root_prefix + os.sep):
        raise ValueError("Invalid function directory")
    for path in Path(directory_real).rglob("*"):
        resolved_real = os.path.realpath(
            os.path.join(directory_real, path.relative_to(directory))
        )
        resolved_path = Path(resolved_real)
        if not resolved_real.startswith(
            directory_real + os.sep
        ) or not resolved_path.is_relative_to(directory):
            raise ValueError("Invalid package path")
        if path.is_symlink():
            raise ValueError("Package symlinks are unsupported")
        if not resolved_real.startswith(directory_real + os.sep):
            raise ValueError("Invalid package path")
        if Path(resolved_real).is_file():
            if not resolved_real.startswith(directory_real + os.sep):
                raise ValueError("Invalid package path")
            with open(resolved_real, "rb") as package_file:
                files[path.relative_to(directory).as_posix()] = package_file.read()
    return manifest, files


class FunctionInvocationService:
    """The shared admission boundary for all function triggers.

    ``enabled`` maps operator-enabled ids to their enabled grants. The supplied
    store must be the one-attempt run-history store, never a delivery queue.
    Actor bindings are trusted authentication-boundary inputs, never headers.
    """

    def __init__(
        self,
        graph_service,
        store,
        *,
        root="config/functions",
        enabled=None,
        runner=None,
        isolation_minimum="degraded",
    ):
        if isolation_minimum not in {"degraded", "full"}:
            raise ValueError("Invalid isolation minimum")
        policy = getattr(store, "_retry", None)
        if policy is not None and policy.max_attempts != 1:
            raise ValueError("Function history requires a one-attempt store")
        self.graph_service = graph_service
        self.store = store
        self.root = root
        self.enabled = enabled if enabled is not None else {}
        if not isinstance(self.enabled, dict) or any(
            not isinstance(key, str)
            or not _ID.fullmatch(key)
            or not isinstance(grants, list)
            or any(
                not isinstance(grant, str) or grant not in KNOWN_GRANTS
                for grant in grants
            )
            for key, grants in self.enabled.items()
        ):
            raise ValueError("Invalid function enablement configuration")
        if self.enabled:
            self._require_durable_history()
        self.runner = runner or HardenedSubprocessRunner()
        self.isolation_minimum = isolation_minimum

    def _require_durable_history(self):
        # Enabled functions must share persistent history across host restarts
        # and processes. The registry's default volatile adapter is insufficient.
        if not isinstance(self.store, SqliteExecutionStore) or self.store.db_path in {
            "",
            ":memory:",
        }:
            raise ValueError(
                "Function invocation requires file-backed SQLite run history; "
                "configure run_history_db"
            )

    def _replay(self, existing, binding):
        # Authentication transport is not identity. Redacted records have no
        # attribution or content left; current graph access gates their replay.
        if not binding.get("actor_id"):
            raise PermissionError("Access denied")
        if existing.redacted_at is None:
            attribution = existing.payload.get("attribution") or {}
            actor = attribution.get("actor") or {}
            scope = attribution.get("scope") or {}
            if (
                actor.get("actor_id") != binding["actor_id"]
                or actor.get("actor_type") != binding["actor_type"]
                or any(
                    scope.get(key) != binding[key]
                    for key in ("workspace_id", "workspace_kind", "graph_id")
                )
            ):
                raise PermissionError("Run belongs to another actor")
        with use_request_authorization(**binding):
            decision = evaluate_graph_access(
                self.graph_service.authorization_hook,
                action="read",
                target="invoke_function",
            )
            if not decision.allowed or (
                binding.get("actor_type") == "function_webhook"
                and not decision.graph_access.matches(graph_id=binding["graph_id"])
            ):
                raise PermissionError("Access denied")
        return existing

    def replay_webhook(self, function_id, idempotency_key, actor_binding):
        if idempotency_key is None:
            return None
        binding = {
            key: actor_binding.get(key)
            for key in (
                "actor_id",
                "actor_type",
                "auth_source",
                "workspace_id",
                "workspace_kind",
                "graph_id",
                "authenticated_principal",
            )
        }
        with _admission(self.root):
            for existing in self.store.list_jobs(
                agent_id=function_id, kind=ExecutionKind.FUNCTION
            ):
                if existing.idempotency_key == f"fn:{function_id}:{idempotency_key}":
                    return self._replay(existing, binding)
        return None

    def invoke(
        self,
        function_id,
        input,
        *,
        trigger="manual",
        actor_binding,
        idempotency_key=None,
        correlation_id=None,
    ) -> RunRecord:
        if not isinstance(function_id, str) or not _ID.fullmatch(function_id):
            raise ValueError("Invalid function id")
        self._require_durable_history()
        started = time.monotonic()
        # Only the trusted binding supplies identity and scope. In particular,
        # never carry its headers through the authorization helper.
        binding = {
            key: actor_binding.get(key)
            for key in (
                "actor_id",
                "actor_type",
                "auth_source",
                "workspace_id",
                "workspace_kind",
                "graph_id",
                "authenticated_principal",
            )
        }
        job = ExecutionJob(
            agent_id=function_id,
            kind=ExecutionKind.FUNCTION,
            idempotency_key=f"fn:{function_id}:{idempotency_key if idempotency_key is not None else uuid.uuid4()}",
            state=ExecutionState.RUNNING,
            attempts=1,
            correlation_id=correlation_id or str(uuid.uuid4()),
            payload={
                "function_id": function_id,
                "trigger": trigger,
                "attribution": {
                    "actor": {
                        key: binding[key]
                        for key in ("actor_id", "actor_type", "auth_source")
                    },
                    "scope": {
                        key: binding[key]
                        for key in ("workspace_id", "workspace_kind", "graph_id")
                    },
                },
            },
        )
        job.started_at = utcnow()
        manifest = None
        code = None
        probe = None
        with _admission(self.root):
            # Enqueue's UNIQUE constraint is the authority for delivery deduplication.
            previous = self.store.list_jobs(
                agent_id=function_id, kind=ExecutionKind.FUNCTION
            )
            for existing in previous:
                if existing.idempotency_key == job.idempotency_key:
                    return self._replay(existing, binding)
            try:
                if function_id not in self.enabled:
                    code = "not_enabled"
                else:
                    manifest, files = load_manifest(self.root, function_id)
                    job.payload["function_version"] = manifest["version"]
                    job.origin = f"function:{function_id}@{manifest['version']}"
                    if not set(manifest["grants"]).issubset(self.enabled[function_id]):
                        code = "not_enabled"
                    elif {"kind": trigger} not in manifest["triggers"]:
                        code = "invalid_manifest"
                    elif not binding.get("actor_id"):
                        code = "access_denied"
                    else:
                        with use_request_authorization(**binding):
                            actions = ["read"] + (
                                ["mutate"]
                                if "graph:write" in manifest["grants"]
                                and (
                                    trigger != "webhook"
                                    or self.webhook_policy.get(
                                        function_id, binding["graph_id"]
                                    )[1]
                                )
                                else []
                            )
                            for action in actions:
                                decision = evaluate_graph_access(
                                    self.graph_service.authorization_hook,
                                    action=action,
                                    target="invoke_function",
                                )
                                if not decision.allowed or (
                                    trigger == "webhook"
                                    and not decision.graph_access.matches(
                                        graph_id=binding["graph_id"]
                                    )
                                ):
                                    code = "access_denied"
                                    break
                        if code is None:
                            try:
                                if not isinstance(input, dict):
                                    raise ValueError("Input must be an object")
                                if "input_schema" in manifest:
                                    _schema(manifest["input_schema"]).validate(input)
                                elif input:
                                    raise ValueError("Input must be empty")
                            except (ValidationError, ValueError):
                                code = "invalid_input"
                if code is None and any(
                    j.state == ExecutionState.RUNNING for j in previous
                ):
                    code = "busy"
                if code is None:
                    try:
                        probe = self.runner.probe()
                    except RunnerUnavailable:
                        code = "isolation_below_minimum"
                    if probe is not None and probe.level == "degraded":
                        logger.warning(
                            "User function isolation is degraded; missing controls: %s",
                            probe.missing,
                        )
                    if (
                        probe is not None
                        and self.isolation_minimum == "full"
                        and probe.level != "full"
                    ):
                        code = "isolation_below_minimum"
            except Exception:  # Fail closed, including unresolvable schema references.
                code = "invalid_manifest"
            if code:
                job.started_at = None
            else:
                job.started_at = utcnow()
            stored = self.store.enqueue(job)
            if stored.id != job.id:
                if (
                    stored.kind != ExecutionKind.FUNCTION
                    or stored.agent_id != function_id
                    or stored.idempotency_key != job.idempotency_key
                ):
                    raise PermissionError("Conflicting run identity")
                return self._replay(stored, binding)
            if code:
                self.store.cancel(
                    job.id,
                    result={
                        "status": "rejected",
                        "error": {"code": code, "message": "Invocation rejected."},
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    },
                )
                return self.store.get(job.id)
        try:
            limits = manifest.get("limits", {})
            spec = RunSpec(
                job.id,
                manifest["runtime"],
                manifest["entry_point"],
                files,
                timeout_seconds=limits.get("timeout_seconds", 120),
                memory_mib=limits.get("memory_mib", 256),
            )
            context = BrokerContext(
                job.id,
                function_id,
                manifest["version"],
                frozenset(manifest["grants"]),
                job.correlation_id,
                binding,
                lambda grant: (
                    function_id in self.enabled
                    and grant in self.enabled[function_id]
                    and (
                        trigger != "webhook"
                        or (
                            self.webhook_policy.get(function_id, binding["graph_id"])[0]
                            and (
                                grant != "graph:write"
                                or self.webhook_policy.get(
                                    function_id, binding["graph_id"]
                                )[1]
                            )
                        )
                    )
                ),
                max_tool_calls=limits.get("max_tool_calls", 200),
            )
            envelope = {
                "schema_version": "1.0",
                "event_id": f"evt-{uuid.uuid4()}",
                "event_type": "function.invoke",
                "occurred_at": job.started_at.isoformat(),
                "entity": None,
                "origin": {
                    "event_origin": trigger,
                    "event_session_id": None,
                    "event_correlation_id": job.correlation_id,
                    "attribution": job.payload["attribution"],
                },
                "invocation": {
                    "contract_version": manifest["contract_version"],
                    "run_id": job.id,
                    "function_id": function_id,
                    "function_version": manifest["version"],
                    "trigger": trigger,
                    "input": input,
                    "idempotency_key": idempotency_key,
                    "chain_depth": 0,
                    "deadline": (
                        job.started_at + timedelta(seconds=spec.timeout_seconds)
                    ).isoformat(),
                },
            }
            outcome = run_function(
                self.runner, spec, FunctionBroker(self.graph_service, context), envelope
            )
            if outcome.status == "succeeded" and "output_schema" in manifest:
                try:
                    _schema(manifest["output_schema"]).validate(outcome.output)
                except ValidationError:
                    outcome.status, outcome.code = "failed", "invalid_output"
                    outcome.output = None
            if outcome.status not in {"succeeded", "failed", "timed_out"}:
                outcome.status, outcome.code = "failed", "runner_lost"
            if outcome.code is not None and outcome.code not in _FAILURE_CODES:
                outcome.status, outcome.code = "failed", "runner_lost"
            result = {
                "status": outcome.status,
                "output": outcome.output,
                "error": {
                    "code": outcome.code,
                    "message": outcome.message[:500] or "Function run failed.",
                    "function_code": outcome.function_error_code[:200],
                }
                if outcome.code
                else None,
                "tool_calls": [asdict(call) for call in outcome.tool_calls],
                "usage": {
                    "wall_ms": outcome.wall_ms,
                    "peak_memory_kib": outcome.peak_memory_kib,
                },
                "isolation": {"level": probe.level, "missing": list(probe.missing)},
                "log": outcome.log,
                "log_truncated": outcome.log_truncated,
            }
        except Exception:
            result = {
                "status": "failed",
                "error": {"code": "runner_lost", "message": "Function runner failed."},
            }
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        if result["status"] == "succeeded":
            return self.store.complete(job.id, result=result)
        # fail has no result argument; keep the structured failure after the
        # contract's code prefix so the unchanged store seam retains diagnostics.
        code = (result.get("error") or {}).get("code") or "runner_lost"
        return self.store.fail(job.id, error=f"{code}: {json.dumps(result)}")
