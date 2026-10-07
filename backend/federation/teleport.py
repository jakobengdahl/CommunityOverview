"""Canonical route resolution for teleporting to a node's source graph.

Federated search puts nodes from remote graphs next to local ones. Teleport is
the navigation step back: given a node a caller is looking at, work out where
that node is actually owned and how to get there.

This module is deliberately pure — it takes the provenance already carried on a
node (see ``FederationManager._build_cache``), the graph's configuration, its
cache health, and the caller's existing graph-access narrowing, and returns one
of a closed set of outcomes. It makes no authorization decision of its own: the
caller passes in the already-evaluated ``GraphAccessNarrowing`` and this module
reports ``permission_denied`` when that narrowing does not admit the graph.

The outcomes are the contract the SaaS layer and the UI both code against:

``local``
    The node is owned by this graph, so there is nowhere to teleport to. A
    local node carries no ``origin_graph_id`` at all, which is exactly what
    ``access.node_graph_id`` reports as ``""`` — the local and federation paths
    agree on one provenance field rather than each having their own notion.
``ok``
    The node is remote, the caller may see its graph, and a route was built.
``permission_denied``
    The caller's narrowing does not admit the source graph. No route is built
    and no endpoint is named, so the response cannot be used to discover that a
    graph exists.
``graph_unavailable``
    The source graph is known but cannot be reached right now: it is disabled,
    it has no ``gui_url`` configured, or its cache is degraded/offline. The
    graph is named, because the caller is already entitled to see it.
``unknown_node``
    Nothing — local or cached — is known by that id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

STATUS_LOCAL = "local"
STATUS_OK = "ok"
STATUS_PERMISSION_DENIED = "permission_denied"
STATUS_GRAPH_UNAVAILABLE = "graph_unavailable"
STATUS_UNKNOWN_NODE = "unknown_node"

#: Cache states that mean the remote graph is not currently answering.
UNAVAILABLE_CACHE_STATES = frozenset({"degraded", "offline", "disabled"})

#: Query parameters of the canonical route. The receiving deployment reads
#: ``node`` to focus the node, and the rest to offer a way back.
PARAM_NODE = "node"
PARAM_RETURN_GRAPH = "from_graph"
PARAM_RETURN_SESSION = "from_session"
PARAM_QUERY = "q"


@dataclass(frozen=True)
class TeleportTarget:
    """The resolved outcome of a teleport request."""

    status: str
    route: Optional[str] = None
    origin_graph_id: str = ""
    origin_graph_name: str = ""
    origin_node_id: str = ""
    #: True when the route leaves this deployment for a different origin.
    cross_deployment: bool = False
    trust_level: str = ""
    #: Why the graph is unavailable, when it is. Never a remote URL.
    reason: str = ""
    #: The route back to where the caller came from, when one could be built.
    backlink: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "success": self.status in {STATUS_OK, STATUS_LOCAL},
            "status": self.status,
            "origin_graph_id": self.origin_graph_id,
            "origin_graph_name": self.origin_graph_name,
            "origin_node_id": self.origin_node_id,
            "cross_deployment": self.cross_deployment,
        }
        if self.route:
            payload["route"] = self.route
        if self.backlink:
            payload["backlink"] = self.backlink
        if self.trust_level:
            payload["trust_level"] = self.trust_level
        if self.reason:
            payload["reason"] = self.reason
        payload.update(self.extra)
        return payload


def _normalize(value: Any) -> str:
    return str(value or "").strip()


def deployment_origin(url: str) -> str:
    """Return the scheme+host+port of ``url``, lowercased, or "" if unparseable.

    Used to decide ``cross_deployment``. Comparing origins rather than full
    URLs means two graphs served as different paths of one deployment are not
    reported as a cross-deployment hop.
    """
    candidate = _normalize(url)
    if not candidate:
        return ""
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def build_route(
    gui_url: str,
    origin_node_id: str,
    *,
    return_graph_id: str = "",
    return_session_id: str = "",
    search_query: str = "",
) -> str:
    """Build the canonical route into ``gui_url`` focused on ``origin_node_id``.

    Existing query parameters on ``gui_url`` are preserved; the teleport
    parameters are appended, and the fragment is dropped because it is not part
    of the addressing contract.
    """
    base = _normalize(gui_url)
    if not base:
        return ""

    parts = urlsplit(base)
    params = [(PARAM_NODE, _normalize(origin_node_id))]
    if return_graph_id:
        params.append((PARAM_RETURN_GRAPH, return_graph_id))
    if return_session_id:
        params.append((PARAM_RETURN_SESSION, return_session_id))
    if search_query:
        params.append((PARAM_QUERY, search_query))

    existing = parts.query
    appended = urlencode([(k, v) for k, v in params if v])
    query = (
        f"{existing}&{appended}" if existing and appended else (existing or appended)
    )

    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def build_backlink(
    local_gui_url: str,
    *,
    node_id: str = "",
    session_id: str = "",
) -> Optional[str]:
    """Build the route back to this deployment, when its own URL is known.

    The open-core app does not know its own public URL, so this returns None
    unless a deployment supplies one. The hosted layer is where that value
    comes from; keeping the function here keeps the parameter names in one
    place for both sides.
    """
    base = _normalize(local_gui_url)
    if not base:
        return None
    route = build_route(base, node_id, return_session_id=session_id)
    return route or None


def resolve_teleport_target(
    *,
    node_metadata: Optional[Dict[str, Any]],
    node_exists: bool,
    graph_access_matches,
    graph_config: Optional[Any] = None,
    cache_status: str = "",
    request_origin: str = "",
    session_id: str = "",
    search_query: str = "",
    local_gui_url: str = "",
) -> TeleportTarget:
    """Resolve where a node's source graph is and how to reach it.

    ``graph_access_matches`` is the caller's already-evaluated narrowing,
    called as ``graph_access_matches(graph_id=...)`` — the same callable shape
    ``GraphAccessNarrowing.matches`` has, so this module never re-implements an
    authorization rule.
    """
    metadata = node_metadata or {}
    origin_graph_id = _normalize(metadata.get("origin_graph_id"))

    # A node with no origin graph is owned here. This is the same field the
    # local path uses for visibility narrowing, so local and federated nodes
    # are classified off one piece of provenance rather than two.
    if not origin_graph_id:
        if not node_exists:
            return TeleportTarget(status=STATUS_UNKNOWN_NODE)
        if not graph_access_matches(graph_id=""):
            return TeleportTarget(
                status=STATUS_PERMISSION_DENIED,
                reason="local_graph_not_visible",
            )
        return TeleportTarget(
            status=STATUS_LOCAL,
            origin_node_id=_normalize(metadata.get("origin_node_id")),
        )

    # Check visibility before reading any endpoint, so a denied caller cannot
    # learn whether the graph is configured, reachable, or where it lives.
    if not graph_access_matches(graph_id=origin_graph_id):
        return TeleportTarget(
            status=STATUS_PERMISSION_DENIED,
            reason="source_graph_not_visible",
        )

    origin_graph_name = _normalize(metadata.get("origin_graph_name"))
    origin_node_id = _normalize(metadata.get("origin_node_id"))

    if graph_config is None:
        return TeleportTarget(
            status=STATUS_GRAPH_UNAVAILABLE,
            origin_graph_id=origin_graph_id,
            origin_graph_name=origin_graph_name,
            origin_node_id=origin_node_id,
            reason="graph_not_configured",
        )

    display_name = _normalize(getattr(graph_config, "display_name", "")) or (
        origin_graph_name
    )
    trust_level = _normalize(getattr(graph_config, "trust_level", ""))

    if not getattr(graph_config, "enabled", False):
        return TeleportTarget(
            status=STATUS_GRAPH_UNAVAILABLE,
            origin_graph_id=origin_graph_id,
            origin_graph_name=display_name,
            origin_node_id=origin_node_id,
            trust_level=trust_level,
            reason="graph_disabled",
        )

    if _normalize(cache_status).lower() in UNAVAILABLE_CACHE_STATES:
        return TeleportTarget(
            status=STATUS_GRAPH_UNAVAILABLE,
            origin_graph_id=origin_graph_id,
            origin_graph_name=display_name,
            origin_node_id=origin_node_id,
            trust_level=trust_level,
            reason="graph_unreachable",
        )

    endpoints = getattr(graph_config, "endpoints", None)
    gui_url = _normalize(getattr(endpoints, "gui_url", "") if endpoints else "")
    if not gui_url:
        return TeleportTarget(
            status=STATUS_GRAPH_UNAVAILABLE,
            origin_graph_id=origin_graph_id,
            origin_graph_name=display_name,
            origin_node_id=origin_node_id,
            trust_level=trust_level,
            reason="no_gui_url_configured",
        )

    if not origin_node_id:
        return TeleportTarget(
            status=STATUS_GRAPH_UNAVAILABLE,
            origin_graph_id=origin_graph_id,
            origin_graph_name=display_name,
            origin_node_id="",
            trust_level=trust_level,
            reason="no_origin_node_id",
        )

    route = build_route(
        gui_url,
        origin_node_id,
        return_graph_id="",
        return_session_id=session_id,
        search_query=search_query,
    )
    if not route:
        return TeleportTarget(
            status=STATUS_GRAPH_UNAVAILABLE,
            origin_graph_id=origin_graph_id,
            origin_graph_name=display_name,
            origin_node_id=origin_node_id,
            trust_level=trust_level,
            reason="no_gui_url_configured",
        )

    target_origin = deployment_origin(gui_url)
    caller_origin = deployment_origin(request_origin)
    # Unknown origins are reported as a cross-deployment hop: the UI's warning
    # is the safe default when we cannot prove the hop stays in place.
    cross_deployment = not (
        target_origin and caller_origin and target_origin == caller_origin
    )

    return TeleportTarget(
        status=STATUS_OK,
        route=route,
        origin_graph_id=origin_graph_id,
        origin_graph_name=display_name,
        origin_node_id=origin_node_id,
        cross_deployment=cross_deployment,
        trust_level=trust_level,
        backlink=build_backlink(
            local_gui_url,
            session_id=session_id,
        ),
    )
