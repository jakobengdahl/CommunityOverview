"""Generic runtime policy, admission, observation and delivery contracts."""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from backend.runtime.authorization import (
    build_graph_authorization_context,
    get_current_request_authorization_inputs,
)
from backend.runtime.errors import GraphWriteFailed

logger = logging.getLogger(__name__)
MAX_ADMISSION_RETRY_SECONDS = 60


def immutable(value: Any) -> Any:
    """Detach JSON-shaped inputs from mutable graph and request objects."""
    # Metadata accepted by storage may be deeper than Python's call stack.
    result = [None]
    pending = [(value, result, 0, False)]
    while pending:
        source, parent, key, finish = pending.pop()
        if finish:
            parent[key] = tuple(source)
        elif isinstance(source, Mapping):
            target = {}
            parent[key] = MappingProxyType(target)
            pending.extend((v, target, k, False) for k, v in source.items())
        elif isinstance(source, (list, tuple)):
            target = [None] * len(source)
            pending.append((target, parent, key, True))
            pending.extend((v, target, i, False) for i, v in enumerate(source))
        else:
            parent[key] = source
    return result[0]


@dataclass(frozen=True)
class RuntimeContext:
    operation: str = ""
    actor: Mapping[str, Any] = field(default_factory=lambda: immutable({}))
    scope: Mapping[str, Any] = field(default_factory=lambda: immutable({}))
    session_id: str = ""
    agent_id: str = ""
    run_id: str = ""
    event_id: str = ""
    delivery_id: str = ""
    attempt: int = 1
    causation_depth: int = 0
    correlation_id: str = ""
    request_id: str = ""
    attribution: Mapping[str, Any] = field(default_factory=lambda: immutable({}))


_execution_context: ContextVar[RuntimeContext | None] = ContextVar(
    "runtime_execution_context", default=None
)
_admission_wait: ContextVar[Any] = ContextVar("runtime_admission_wait", default=None)


def runtime_context(operation: str, *, event_context=None, **values) -> RuntimeContext:
    context = _execution_context.get()
    if context is None:
        auth = build_graph_authorization_context(action="mutate", target=operation)
        headers = get_current_request_authorization_inputs().get("headers") or {}
        context = RuntimeContext(
            actor=immutable(auth.actor),
            scope=immutable(auth.scope),
            session_id=auth.session_id,
            request_id=headers.get("x-request-id", ""),
            attribution=immutable({"actor": auth.actor, "scope": auth.scope}),
        )
    # Approved actions already restore their server-owned mutation context.
    from backend.core.events.causation import current_agent_mutation_context

    origin = current_agent_mutation_context() or event_context
    if origin is not None:
        context = replace(
            context,
            session_id=origin.event_session_id or context.session_id,
            correlation_id=origin.event_correlation_id or context.correlation_id,
            causation_depth=origin.event_causation_depth,
            attribution=immutable(
                origin.attribution.to_dict() if origin.attribution else {}
            ),
        )
    return replace(context, operation=operation, **values)


@contextmanager
def bind_execution_context(context: RuntimeContext | None):
    token = _execution_context.set(context)
    try:
        yield
    finally:
        _execution_context.reset(token)


@dataclass(frozen=True)
class ResourceChange:
    kind: str
    resource_id: str
    before: Mapping[str, Any] | None
    after: Mapping[str, Any] | None

    @classmethod
    def snapshot(cls, kind, resource_id, before, after):
        # Vectors are not policy inputs; types, revisions and links are retained.
        def document(value):
            return (
                None
                if value is None
                else immutable({k: v for k, v in value.items() if k != "embedding"})
            )

        return cls(kind, resource_id, document(before), document(after))


@dataclass(frozen=True)
class MutationPolicyDecision:
    allowed: bool = True
    code: str = "allowed"


class MutationPolicyHook(Protocol):
    def evaluate(
        self, context: RuntimeContext, change_set: tuple[ResourceChange, ...]
    ) -> MutationPolicyDecision: ...


class DefaultMutationPolicyHook:
    def evaluate(self, context, change_set):
        return MutationPolicyDecision()


class MutationPolicyDenied(GraphWriteFailed):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Mutation policy denied: {code}")


@dataclass(frozen=True)
class AdmissionDecision:
    outcome: str = "allow"
    code: str = "allowed"
    retry_at: float | None = None  # UTC Unix seconds; bounded by the core.


class ExecutionAdmissionHook(Protocol):
    def admit(self, context: RuntimeContext) -> AdmissionDecision: ...


class DefaultExecutionAdmissionHook:
    def admit(self, context):
        return AdmissionDecision()


class AdmissionRefused(Exception):
    def __init__(self, decision: AdmissionDecision):
        self.decision = decision
        self.code = decision.code
        self.retry_at = decision.retry_at
        super().__init__(f"Execution {decision.outcome}: {decision.code}")


@dataclass(frozen=True)
class RuntimeEvent:
    event_type: str
    reason: str
    context: RuntimeContext
    resource_ids: tuple[str, ...] = ()
    version: int = 1
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class RuntimeEventSink(Protocol):
    def emit(self, event: RuntimeEvent) -> None: ...


class DefaultRuntimeEventSink:
    def emit(self, event):
        pass


class _IsolatedSink:
    """Bound memory and threads even when an observer never returns."""

    def __init__(self, sink):
        self._sink = sink
        self._queue = queue.Queue(maxsize=128)
        threading.Thread(target=self._drain, daemon=True, name="runtime-events").start()

    def emit(self, event):
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            pass

    def _drain(self):
        while True:
            event = self._queue.get()
            try:
                self._sink.emit(event)
            except Exception:
                logger.warning("Runtime observer failed", exc_info=True)
            finally:
                self._queue.task_done()


@dataclass(frozen=True)
class DeliveryOutcome:
    outcome: str  # delivered, deferred (adapter owns retry), terminal_failure
    delivery_id: str
    correlation_id: str = ""
    code: str = ""


class EventDeliveryAdapter(Protocol):
    def deliver(
        self, subscription, event, context: RuntimeContext
    ) -> DeliveryOutcome: ...


class DefaultEventDeliveryAdapter:
    """Hand off to the existing HTTP queue, including its retry and SSRF checks."""

    def __init__(self, enqueue):
        self._enqueue = enqueue

    def deliver(self, subscription, event, context):
        self._enqueue(event, subscription["delivery"].webhook_url)
        return DeliveryOutcome(
            "deferred",
            context.delivery_id,
            context.correlation_id,
            "http_queued",
        )


class RuntimeExtensions:
    """Constructor injection, with a process default for independently built clients."""

    def __init__(
        self,
        *,
        mutation_policy=None,
        execution_admission=None,
        event_sink=None,
        event_delivery=None,
    ):
        self.mutation_policy = mutation_policy or DefaultMutationPolicyHook()
        self.execution_admission = (
            execution_admission or DefaultExecutionAdmissionHook()
        )
        self.event_sink = (
            _IsolatedSink(event_sink)
            if event_sink is not None
            else DefaultRuntimeEventSink()
        )
        self.event_delivery = event_delivery

    def evaluate_mutation(self, context, changes):
        changes = tuple(changes)
        if not changes:
            return
        try:
            decision = self.mutation_policy.evaluate(context, changes)
            if (
                not isinstance(decision, MutationPolicyDecision)
                or type(decision.allowed) is not bool
                or not isinstance(decision.code, str)
                or not decision.code
            ):
                raise ValueError("Invalid mutation policy decision")
        except Exception:
            decision = MutationPolicyDecision(False, "mutation_policy_error")
        self.event_sink.emit(
            RuntimeEvent(
                "mutation_policy.allowed"
                if decision.allowed
                else "mutation_policy.denied",
                decision.code,
                context,
                tuple(c.resource_id for c in changes),
            )
        )
        if not decision.allowed:
            raise MutationPolicyDenied(decision.code)

    def admit(self, context):
        try:
            decision = self.execution_admission.admit(context)
            if (
                not isinstance(decision, AdmissionDecision)
                or decision.outcome not in {"allow", "defer", "deny"}
                or not isinstance(decision.code, str)
                or not decision.code
            ):
                raise ValueError("Invalid admission decision")
            if decision.outcome == "defer":
                if decision.retry_at is None or not math.isfinite(decision.retry_at):
                    raise ValueError("A deferral needs a finite retry time")
                now = time.time()
                decision = replace(
                    decision,
                    retry_at=max(
                        now + 0.01,
                        min(decision.retry_at, now + MAX_ADMISSION_RETRY_SECONDS),
                    ),
                )
            return decision
        except Exception:
            return AdmissionDecision("deny", "execution_admission_error")

    def require_admission(self, context):
        decision = self.admit(context)
        wait = _admission_wait.get()
        while decision.outcome == "defer" and wait is not None:
            if wait(max(0, decision.retry_at - time.time())):
                raise AdmissionRefused(AdmissionDecision("deny", "execution_stopped"))
            context = replace(context, attempt=context.attempt + 1)
            decision = self.admit(context)
        if decision.outcome != "allow":
            raise AdmissionRefused(decision)


_default_extensions = RuntimeExtensions()
_bound_extensions: ContextVar[RuntimeExtensions | None] = ContextVar(
    "runtime_extensions", default=None
)


def configure_runtime_extensions(extensions: RuntimeExtensions) -> None:
    """Register at process startup, before constructing stores and workers."""
    global _default_extensions
    _default_extensions = extensions


def get_runtime_extensions() -> RuntimeExtensions:
    return _bound_extensions.get() or _default_extensions


@contextmanager
def bind_runtime_extensions(extensions, *, admission_wait=None):
    token = _bound_extensions.set(extensions)
    wait_token = _admission_wait.set(admission_wait)
    try:
        yield
    finally:
        _admission_wait.reset(wait_token)
        _bound_extensions.reset(token)
