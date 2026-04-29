"""Helpers for generic request actor and scope context resolution."""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

ACTOR_ID_ENV = "COMMUNITYOVERVIEW_ACTOR_ID"
ACTOR_TYPE_ENV = "COMMUNITYOVERVIEW_ACTOR_TYPE"
AUTH_SOURCE_ENV = "COMMUNITYOVERVIEW_AUTH_SOURCE"
WORKSPACE_ID_ENV = "COMMUNITYOVERVIEW_WORKSPACE_ID"
WORKSPACE_KIND_ENV = "COMMUNITYOVERVIEW_WORKSPACE_KIND"
GRAPH_SCOPE_ID_ENV = "COMMUNITYOVERVIEW_GRAPH_SCOPE_ID"

ACTOR_ID_HEADER = "x-communityoverview-actor-id"
ACTOR_TYPE_HEADER = "x-communityoverview-actor-type"
AUTH_SOURCE_HEADER = "x-communityoverview-auth-source"
WORKSPACE_ID_HEADER = "x-communityoverview-workspace-id"
WORKSPACE_KIND_HEADER = "x-communityoverview-workspace-kind"
GRAPH_SCOPE_ID_HEADER = "x-communityoverview-graph-id"


def _clean_string(value: Optional[Any]) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _coerce_headers(headers: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    if not headers:
        return {}
    return {str(key).lower(): _clean_string(value) for key, value in headers.items()}


def _source_from_values(*values: str, preferred: str) -> str:
    return preferred if any(values) else "default"


def get_request_actor_context(
    *,
    headers: Optional[Mapping[str, Any]] = None,
    actor_id: Optional[str] = None,
    actor_type: Optional[str] = None,
    auth_source: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve safe request actor context from env and optional request inputs."""
    normalized_headers = _coerce_headers(headers)

    env_actor_id = _clean_string(os.getenv(ACTOR_ID_ENV))
    env_actor_type = _clean_string(os.getenv(ACTOR_TYPE_ENV))
    env_auth_source = _clean_string(os.getenv(AUTH_SOURCE_ENV))

    resolved_actor_id = _clean_string(actor_id) or normalized_headers.get(ACTOR_ID_HEADER, "") or env_actor_id
    resolved_actor_type = _clean_string(actor_type) or normalized_headers.get(ACTOR_TYPE_HEADER, "") or env_actor_type
    resolved_auth_source = _clean_string(auth_source) or normalized_headers.get(AUTH_SOURCE_HEADER, "") or env_auth_source

    source = "override" if any(_clean_string(value) for value in (actor_id, actor_type, auth_source)) else "request"
    if source == "request" and not any(normalized_headers.get(name, "") for name in (ACTOR_ID_HEADER, ACTOR_TYPE_HEADER, AUTH_SOURCE_HEADER)):
        source = _source_from_values(env_actor_id, env_actor_type, env_auth_source, preferred="environment")

    is_authenticated = bool(resolved_actor_id)
    if not resolved_auth_source:
        resolved_auth_source = "anonymous" if not is_authenticated else "provided"

    return {
        "actor_id": resolved_actor_id,
        "actor_type": resolved_actor_type,
        "is_authenticated": is_authenticated,
        "auth_source": resolved_auth_source,
        "source": source,
    }


def get_request_scope_context(
    *,
    headers: Optional[Mapping[str, Any]] = None,
    workspace_id: Optional[str] = None,
    workspace_kind: Optional[str] = None,
    graph_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve safe request scope context from env and optional request inputs."""
    normalized_headers = _coerce_headers(headers)

    env_workspace_id = _clean_string(os.getenv(WORKSPACE_ID_ENV))
    env_workspace_kind = _clean_string(os.getenv(WORKSPACE_KIND_ENV))
    env_graph_id = _clean_string(os.getenv(GRAPH_SCOPE_ID_ENV))

    resolved_workspace_id = _clean_string(workspace_id) or normalized_headers.get(WORKSPACE_ID_HEADER, "") or env_workspace_id
    resolved_workspace_kind = _clean_string(workspace_kind) or normalized_headers.get(WORKSPACE_KIND_HEADER, "") or env_workspace_kind
    resolved_graph_id = _clean_string(graph_id) or normalized_headers.get(GRAPH_SCOPE_ID_HEADER, "") or env_graph_id

    source = "override" if any(_clean_string(value) for value in (workspace_id, workspace_kind, graph_id)) else "request"
    if source == "request" and not any(normalized_headers.get(name, "") for name in (WORKSPACE_ID_HEADER, WORKSPACE_KIND_HEADER, GRAPH_SCOPE_ID_HEADER)):
        source = _source_from_values(env_workspace_id, env_workspace_kind, env_graph_id, preferred="environment")

    return {
        "workspace_id": resolved_workspace_id,
        "workspace_kind": resolved_workspace_kind,
        "graph_id": resolved_graph_id,
        "source": source,
    }
