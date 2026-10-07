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
    The source graph cannot be opened: it is disabled, federation is off, it is
    no longer configured, its cache is degraded/offline, it has no ``gui_url``,
    its ``gui_url`` is not an http(s) URL, or the node carries no origin id. The
    ``reason`` field says which. The graph is named, because the caller is
    already entitled to see it.
``unknown_node``
    Nothing — local or cached — is known by that id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

STATUS_LOCAL = "local"
STATUS_OK = "ok"
STATUS_PERMISSION_DENIED = "permission_denied"
STATUS_GRAPH_UNAVAILABLE = "graph_unavailable"
STATUS_UNKNOWN_NODE = "unknown_node"

#: Cache states that mean the remote graph is not currently answering.
UNAVAILABLE_CACHE_STATES = frozenset({"degraded", "offline", "disabled"})

#: The single denial reason. Deliberately does not distinguish the local graph
#: from a remote one — see ``resolve_teleport_target``.
REASON_NOT_VISIBLE = "not_visible"

#: A cached federated node's id is ``federated::<graph_id>::<origin_node_id>``.
#: The format lives here, and ``FederationManager._build_cache`` builds ids with
#: it, because teleport has to read the graph id back out of an id whose node is
#: not (or no longer) cached — that is the only classifier available then.
FEDERATED_ID_PREFIX = "federated"
FEDERATED_ID_SEPARATOR = "::"

#: Schemes a route may use. The resolved route is handed to ``window.open``, so
#: anything else — ``javascript:``, ``vbscript:``, ``file:`` and friends, which
#: parse as absolute whenever they carry a host — is not a route to a graph.
ROUTE_SCHEMES = frozenset({"http", "https"})

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


def build_federated_node_id(graph_id: str, origin_node_id: Any) -> str:
    """Compose the cache id for a node fetched from ``graph_id``."""
    return FEDERATED_ID_SEPARATOR.join(
        (FEDERATED_ID_PREFIX, str(graph_id), str(origin_node_id))
    )


def parse_federated_node_id(node_id: str) -> tuple[str, str]:
    """Split a federated cache id into ``(graph_id, origin_node_id)``.

    Returns ``("", "")`` for anything that is not one, so a local id simply has
    no graph to read. The origin id keeps any separator it contained, since only
    the graph id is delimited.
    """
    candidate = _normalize(node_id)
    parts = candidate.split(FEDERATED_ID_SEPARATOR, 2)
    if len(parts) != 3 or parts[0] != FEDERATED_ID_PREFIX:
        return "", ""
    return parts[1].strip(), parts[2]


def deployment_origin(url: str) -> str:
    """Return the scheme+host+port of ``url``, lowercased, or "" if it is not one.

    Used both to decide ``cross_deployment`` and to decide whether a configured
    URL is a route at all. Comparing origins rather than full URLs means two
    graphs served as different paths of one deployment are not reported as a
    cross-deployment hop. Only ``http`` and ``https`` qualify: a script or file
    URL carrying a host parses as absolute but is not somewhere a graph lives,
    and the route it would produce ends up in ``window.open``.
    """
    candidate = _normalize(url)
    if not candidate:
        return ""
    try:
        parts = urlsplit(candidate)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme.lower() not in ROUTE_SCHEMES or not host:
        return ""
    # hostname/port rather than netloc, so credentials in a configured URL are
    # not part of the identity of a deployment: two URLs differing only in
    # userinfo are the same origin, not a cross-deployment hop.
    authority = host.lower()
    if ":" in authority:  # IPv6 literal — keep it bracketed
        authority = f"[{authority}]"
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{parts.scheme.lower()}://{authority}"


def build_route(
    gui_url: str,
    origin_node_id: str,
    *,
    return_graph_id: str = "",
    return_session_id: str = "",
    search_query: str = "",
) -> str:
    """Build the canonical route into ``gui_url`` focused on ``origin_node_id``.

    Query parameters already on ``gui_url`` are kept, but a teleport parameter
    *replaces* one of the same name rather than being appended after it: the
    receiving end reads the first value of a repeated parameter, so appending
    would let a ``gui_url`` configured with its own ``node=`` shadow the node
    this route exists to focus. Mirrors ``config_loader.build_session_url``,
    which merges the same way. The fragment is dropped because it is not part
    of the addressing contract.
    """
    base = _normalize(gui_url)
    if not base:
        return ""

    parts = urlsplit(base)
    params = [
        (PARAM_NODE, _normalize(origin_node_id)),
        (PARAM_RETURN_GRAPH, _normalize(return_graph_id)),
        (PARAM_RETURN_SESSION, _normalize(return_session_id)),
        (PARAM_QUERY, _normalize(search_query)),
    ]

    # The builder owns these four keys: a value it has is written, a value it
    # does not have is *removed* rather than inherited from the configured URL.
    # Inheriting would let a base URL carrying its own node= survive into a
    # backlink, which addresses no node at all.
    owned = {key for key, _ in params}
    teleport_params = {key: value for key, value in params if value}
    merged = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in owned
    ]
    merged.extend(teleport_params.items())

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(merged), ""))


def build_backlink(
    local_gui_url: str,
    *,
    session_id: str = "",
) -> Optional[str]:
    """Build the route back to this deployment, when its own URL is known.

    A standalone deployment has no configured public URL, so this returns None
    rather than emitting a guessed or ``localhost`` one — the same rule
    ``config_loader.build_session_url`` follows. A configured value that is not
    an absolute URL is refused for the same reason the target ``gui_url`` is:
    the receiving deployment would resolve it against its own origin, which is
    not where the visitor came from. Keeping the function here keeps the
    parameter names in one place for both ends of the hop.
    """
    base = _normalize(local_gui_url)
    if not base or not deployment_origin(base):
        return None
    route = build_route(base, "", return_session_id=session_id)
    return route or None


def resolve_teleport_target(
    *,
    node_metadata: Optional[Dict[str, Any]],
    node_exists: bool,
    graph_access_matches,
    node_id: str = "",
    graph_config: Optional[Any] = None,
    cache_status: str = "",
    session_id: str = "",
    search_query: str = "",
    local_gui_url: str = "",
    local_graph_name: str = "",
) -> TeleportTarget:
    """Resolve where a node's source graph is and how to reach it.

    ``graph_access_matches`` is the caller's already-evaluated narrowing,
    called as ``graph_access_matches(graph_id=...)`` — the same callable shape
    ``GraphAccessNarrowing.matches`` has, so this module never re-implements an
    authorization rule.

    ``local_gui_url`` is this deployment's configured public URL. It is the
    origin a route is compared against as well as the base of the backlink;
    nothing derived from the request is used, because a request's own host comes
    from a header the caller controls.
    """
    metadata = node_metadata or {}
    # The requested id is the classifier of last resort: a node that is not
    # found has no metadata to read, and without this the visibility check
    # would fall through to the local-graph check and answer "no such node" for
    # a graph the caller may not see — an existence oracle for that graph.
    requested_graph_id, _ = parse_federated_node_id(node_id)
    origin_graph_id = _normalize(metadata.get("origin_graph_id")) or _normalize(
        requested_graph_id
    )

    # Visibility comes first — before the node is known to exist, and before
    # any endpoint or graph field is read. A federated id embeds its graph id
    # (``federated::<graph_id>::<origin_id>``), so answering "no such node" for
    # an invisible graph and "denied" for a visible-but-forbidden one would
    # confirm, to anyone able to probe ids, which graphs are configured and
    # synced. Every id naming a graph the narrowing excludes gets the same
    # answer, with the same single reason for the local and the remote case.
    if not graph_access_matches(graph_id=origin_graph_id):
        return TeleportTarget(
            status=STATUS_PERMISSION_DENIED,
            reason=REASON_NOT_VISIBLE,
        )

    # Only then: nothing to resolve for an id that names no node, whatever
    # metadata a caller happened to pass alongside it.
    if not node_exists:
        return TeleportTarget(status=STATUS_UNKNOWN_NODE)

    # A node with no origin graph is owned here. This is the same field the
    # local path uses for visibility narrowing, so local and federated nodes
    # are classified off one piece of provenance rather than two.
    if not origin_graph_id:
        return TeleportTarget(
            status=STATUS_LOCAL,
            origin_node_id=_normalize(metadata.get("origin_node_id")),
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

    target_origin = deployment_origin(gui_url)
    if not target_origin:
        # A gui_url without a scheme and host is not a route anywhere: the
        # browser would resolve it against the *caller's* own origin, so the
        # user would confirm leaving for another deployment and land back on
        # their own at a bogus path. Omitting the scheme is an easy operator
        # mistake, so it degrades like any other unreachable graph. This also
        # makes cross_deployment meaningful whenever the status is ok.
        return TeleportTarget(
            status=STATUS_GRAPH_UNAVAILABLE,
            origin_graph_id=origin_graph_id,
            origin_graph_name=display_name,
            origin_node_id=origin_node_id,
            trust_level=trust_level,
            reason="gui_url_not_absolute",
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
        # Open core has no graph *id* for itself — local is the empty id — so
        # the route names this graph the way the UI names it, which is what the
        # receiving end displays.
        return_graph_id=_normalize(local_graph_name),
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

    # target_origin is non-empty by the guard above, so this is a real
    # comparison. A deployment with no configured public URL has no origin of
    # its own to compare, so every hop reports as cross-deployment: the UI's
    # confirmation is the safe default when we cannot prove the hop stays in
    # place, and the alternative — trusting the request's host header — would
    # let a caller suppress that confirmation.
    caller_origin = deployment_origin(local_gui_url)
    cross_deployment = target_origin != caller_origin

    return TeleportTarget(
        status=STATUS_OK,
        route=route,
        origin_graph_id=origin_graph_id,
        origin_graph_name=display_name,
        origin_node_id=origin_node_id,
        cross_deployment=cross_deployment,
        trust_level=trust_level,
        backlink=build_backlink(local_gui_url, session_id=session_id),
    )
