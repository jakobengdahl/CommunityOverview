"""Generic authorization seam for graph access decisions."""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Mapping, Optional, Protocol

from backend.request_context import get_request_actor_context, get_request_scope_context

AUTHORIZATION_MODE_ENV = "COMMUNITYOVERVIEW_AUTHORIZATION_MODE"
AUTHORIZATION_MODE_PERMISSIVE = "permissive"
AUTHORIZATION_MODE_READ_ONLY = "read-only"
AUTHORIZATION_MODE_DENY_ALL = "deny-all"

GRAPH_ACTION_READ = "read"
GRAPH_ACTION_MUTATE = "mutate"

_request_authorization_inputs: ContextVar[Dict[str, Any]] = ContextVar(
    "communityoverview_request_authorization_inputs",
    default={},
)


def _clean_string(value: Optional[Any]) -> str:
    if value is None:
        return ""
    return str(value).strip()


@dataclass(frozen=True)
class GraphAuthorizationContext:
    """Normalized context for a graph authorization decision."""

    action: str
    target: str
    actor: Dict[str, Any]
    scope: Dict[str, Any]
    graph_id: str


@dataclass(frozen=True)
class GraphAuthorizationDecision:
    """Result of a graph authorization evaluation."""

    allowed: bool
    reason: str = ""
    mode: str = AUTHORIZATION_MODE_PERMISSIVE
    source: str = "default"


class GraphAuthorizationHook(Protocol):
    """Protocol for hosted or plugin-provided graph authorization hooks."""

    def evaluate(self, context: GraphAuthorizationContext) -> GraphAuthorizationDecision:
        ...


class DefaultGraphAuthorizationHook:
    """Standalone-safe default hook with optional environment narrowing."""

    def evaluate(self, context: GraphAuthorizationContext) -> GraphAuthorizationDecision:
        mode = _clean_string(os.getenv(AUTHORIZATION_MODE_ENV)).lower() or AUTHORIZATION_MODE_PERMISSIVE

        if mode == AUTHORIZATION_MODE_DENY_ALL:
            return GraphAuthorizationDecision(
                allowed=False,
                reason="Graph access is disabled by the current authorization mode.",
                mode=mode,
                source="environment",
            )

        if mode == AUTHORIZATION_MODE_READ_ONLY and context.action == GRAPH_ACTION_MUTATE:
            return GraphAuthorizationDecision(
                allowed=False,
                reason="Graph mutations are disabled by the current authorization mode.",
                mode=mode,
                source="environment",
            )

        return GraphAuthorizationDecision(
            allowed=True,
            mode=mode if mode in {AUTHORIZATION_MODE_READ_ONLY, AUTHORIZATION_MODE_DENY_ALL} else AUTHORIZATION_MODE_PERMISSIVE,
            source="environment" if _clean_string(os.getenv(AUTHORIZATION_MODE_ENV)) else "default",
        )


def get_current_request_authorization_inputs() -> Dict[str, Any]:
    """Return request-scoped authorization inputs if one has been set."""
    return dict(_request_authorization_inputs.get())


@contextmanager
def use_request_authorization(
    *,
    headers: Optional[Mapping[str, Any]] = None,
    actor_id: Optional[str] = None,
    actor_type: Optional[str] = None,
    auth_source: Optional[str] = None,
    workspace_id: Optional[str] = None,
    workspace_kind: Optional[str] = None,
    graph_id: Optional[str] = None,
) -> Iterator[None]:
    """Temporarily bind request-level authorization inputs for service calls."""
    token = _request_authorization_inputs.set({
        "headers": headers,
        "actor_id": actor_id,
        "actor_type": actor_type,
        "auth_source": auth_source,
        "workspace_id": workspace_id,
        "workspace_kind": workspace_kind,
        "graph_id": graph_id,
    })
    try:
        yield
    finally:
        _request_authorization_inputs.reset(token)


def build_graph_authorization_context(*, action: str, target: str) -> GraphAuthorizationContext:
    """Build a graph authorization context from request-safe inputs and environment defaults."""
    inputs = get_current_request_authorization_inputs()
    actor = get_request_actor_context(
        headers=inputs.get("headers"),
        actor_id=inputs.get("actor_id"),
        actor_type=inputs.get("actor_type"),
        auth_source=inputs.get("auth_source"),
    )
    scope = get_request_scope_context(
        headers=inputs.get("headers"),
        workspace_id=inputs.get("workspace_id"),
        workspace_kind=inputs.get("workspace_kind"),
        graph_id=inputs.get("graph_id"),
    )
    return GraphAuthorizationContext(
        action=action,
        target=target,
        actor=actor,
        scope=scope,
        graph_id=scope.get("graph_id", ""),
    )