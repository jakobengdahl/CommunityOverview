"""Tests for canonical teleport route resolution.

The invariants under test are the four behaviours the navigation contract
defines (permission denial, unavailable graph, cross-deployment, backlink),
plus the local/federated classification that decides between them.
"""

from urllib.parse import parse_qs, urlsplit

import pytest

from backend.federation import teleport
from backend.federation.config import FederationGraphConfig


def _graph(
    graph_id="remote-a",
    display_name="Remote A",
    enabled=True,
    gui_url="https://graph-a.example/app",
    trust_level="partner",
):
    return FederationGraphConfig.model_validate(
        {
            "graph_id": graph_id,
            "display_name": display_name,
            "enabled": enabled,
            "trust_level": trust_level,
            "endpoints": {
                "graph_json_url": "https://graph-a.example/graph.json",
                "gui_url": gui_url,
            },
        }
    )


def _federated_metadata(
    origin_graph_id="remote-a",
    origin_graph_name="Remote A",
    origin_node_id="node-7",
):
    return {
        "origin_graph_id": origin_graph_id,
        "origin_graph_name": origin_graph_name,
        "origin_node_id": origin_node_id,
        "federation_distance": 1,
        "is_federated": True,
    }


def _allow_all(*, graph_id):
    return True


def _deny_all(*, graph_id):
    return False


def _allow_only(*allowed):
    def matches(*, graph_id):
        return graph_id in allowed

    return matches


def _resolve(**overrides):
    kwargs = {
        "node_metadata": _federated_metadata(),
        "node_exists": True,
        "graph_access_matches": _allow_all,
        "graph_config": _graph(),
        "cache_status": "healthy",
        "session_id": "",
        "search_query": "",
        # No configured public URL by default, so a hop reports as
        # cross-deployment unless a test supplies this deployment's own origin.
        "local_gui_url": "",
    }
    kwargs.update(overrides)
    return teleport.resolve_teleport_target(**kwargs)


# ---------------------------------------------------------------------------
# Local vs federated classification
# ---------------------------------------------------------------------------


def test_a_node_without_an_origin_graph_is_owned_here():
    target = _resolve(node_metadata={"some": "value"})

    assert target.status == teleport.STATUS_LOCAL
    assert target.route is None
    assert target.to_dict()["success"] is True


def test_a_node_with_no_metadata_at_all_is_owned_here():
    target = _resolve(node_metadata=None)

    assert target.status == teleport.STATUS_LOCAL


def test_the_is_federated_marker_does_not_make_a_node_remote():
    # origin_graph_id is the single classifier on the backend as well as the
    # frontend (nodeProvenance.test.js pins the other side). A node carrying the
    # marker but no origin graph is owned here, so the user is told they are
    # already in the right graph rather than that a source graph is unreachable.
    target = _resolve(
        node_metadata={"is_federated": True, "federation_distance": 1},
    )

    assert target.status == teleport.STATUS_LOCAL


def test_an_empty_origin_graph_id_is_treated_as_local_not_as_a_remote_graph():
    target = _resolve(node_metadata={"origin_graph_id": "   "})

    assert target.status == teleport.STATUS_LOCAL


def test_an_unknown_node_is_reported_as_unknown_rather_than_local():
    target = _resolve(node_metadata=None, node_exists=False)

    assert target.status == teleport.STATUS_UNKNOWN_NODE
    assert target.to_dict()["success"] is False


def test_a_missing_node_is_unknown_even_when_federated_metadata_is_passed():
    # node_exists is checked before the local/remote split, so an id that names
    # no node cannot be routed on the strength of metadata alone. The narrowing
    # admits this graph, so the answer is about the node rather than access.
    target = _resolve(node_exists=False, graph_access_matches=_allow_all)

    assert target.status == teleport.STATUS_UNKNOWN_NODE
    assert target.route is None


def test_a_federated_node_resolves_to_a_route_into_its_source_graph():
    target = _resolve()

    assert target.status == teleport.STATUS_OK
    assert target.origin_graph_id == "remote-a"
    assert target.origin_graph_name == "Remote A"
    assert target.origin_node_id == "node-7"
    assert urlsplit(target.route).path == "/app"
    assert parse_qs(urlsplit(target.route).query)["node"] == ["node-7"]


# ---------------------------------------------------------------------------
# Behaviour 1: permission denial
# ---------------------------------------------------------------------------


def test_a_denied_caller_gets_no_route():
    target = _resolve(graph_access_matches=_deny_all)

    assert target.status == teleport.STATUS_PERMISSION_DENIED
    assert target.route is None


def test_a_denied_caller_learns_nothing_about_the_graph_it_may_not_see():
    target = _resolve(graph_access_matches=_deny_all)
    payload = target.to_dict()

    assert payload["origin_graph_id"] == ""
    assert payload["origin_graph_name"] == ""
    assert payload["origin_node_id"] == ""
    assert "route" not in payload
    assert "trust_level" not in payload
    serialized = repr(payload)
    assert "graph-a.example" not in serialized
    assert "remote-a" not in serialized
    assert "Remote A" not in serialized


def test_denial_is_decided_before_an_unavailable_graph_is_reported():
    # Otherwise "unavailable" vs "denied" would tell a caller which graphs are
    # configured, which is the leak the ordering exists to prevent.
    target = _resolve(
        graph_access_matches=_deny_all,
        graph_config=None,
        cache_status="offline",
    )

    assert target.status == teleport.STATUS_PERMISSION_DENIED
    assert target.reason == teleport.REASON_NOT_VISIBLE


def test_the_denial_reason_does_not_say_whether_the_graph_was_local_or_remote():
    # A reason that distinguished the two would tell a caller probing ids that
    # the node belongs to a graph other than the local one, and so that a graph
    # they cannot see exists.
    local = _resolve(node_metadata={}, graph_access_matches=_deny_all)
    remote = _resolve(graph_access_matches=_deny_all)

    assert local.status == remote.status == teleport.STATUS_PERMISSION_DENIED
    assert local.reason == remote.reason == teleport.REASON_NOT_VISIBLE
    assert local.to_dict() == remote.to_dict()


def test_denial_does_not_reveal_whether_the_node_exists():
    # A federated id embeds its graph id, so answering "no such node" for an
    # invisible graph and "denied" for a visible one would confirm which graphs
    # are configured and synced to anyone able to probe ids.
    #
    # A missing node reaches the resolver with no metadata at all — that is what
    # queries.resolve_teleport passes — so the absent case is given that shape
    # rather than a metadata dict it could never carry. Pinning the
    # metadata-rich pair instead is what let this leak read as closed.
    present = _resolve(
        graph_access_matches=_deny_all,
        node_exists=True,
        node_id="federated::remote-a::node-7",
    )
    absent = teleport.resolve_teleport_target(
        node_metadata=None,
        node_exists=False,
        graph_access_matches=_deny_all,
        node_id="federated::remote-a::no-such-node",
        graph_config=None,
    )

    assert present.status == absent.status == teleport.STATUS_PERMISSION_DENIED
    assert present.to_dict() == absent.to_dict()


def test_the_requested_id_supplies_the_graph_when_the_node_is_missing():
    target = teleport.resolve_teleport_target(
        node_metadata=None,
        node_exists=False,
        graph_access_matches=_allow_only("other-graph"),
        node_id="federated::remote-a::no-such-node",
    )

    assert target.status == teleport.STATUS_PERMISSION_DENIED


def test_a_missing_local_id_is_an_unknown_node():
    target = teleport.resolve_teleport_target(
        node_metadata=None,
        node_exists=False,
        graph_access_matches=_allow_all,
        node_id="local-1",
    )

    assert target.status == teleport.STATUS_UNKNOWN_NODE


def test_the_nodes_own_provenance_wins_over_the_requested_id():
    # An adoption reference stub is a local node keyed by a federated id; its
    # metadata is the authority on which graph owns it.
    target = _resolve(
        node_metadata=_federated_metadata(origin_graph_id="remote-a"),
        node_id="federated::some-other-graph::node-7",
        graph_access_matches=_allow_only("remote-a"),
    )

    assert target.status == teleport.STATUS_OK
    assert target.origin_graph_id == "remote-a"


def test_an_existing_node_with_no_provenance_is_local_whatever_its_id_looks_like():
    # The id is caller-supplied and any mutating caller may choose it, so it
    # must not speak for a node that is really here. The fallback exists only
    # for a node that was not found.
    target = _resolve(
        node_metadata={},
        node_exists=True,
        node_id="federated::esam-main::spoof",
    )

    assert target.status == teleport.STATUS_LOCAL
    assert target.origin_graph_id == ""


def test_an_existing_local_node_is_not_denied_on_the_strength_of_its_id():
    target = _resolve(
        node_metadata={},
        node_exists=True,
        node_id="federated::some-graph::spoof",
        graph_access_matches=_allow_only(""),
    )

    assert target.status == teleport.STATUS_LOCAL


@pytest.mark.parametrize(
    "node_id,expected",
    [
        ("federated::esam-main::remote-1", ("esam-main", "remote-1")),
        # An empty graph segment carries no graph, so it falls back to local
        # rather than naming a graph called "".
        ("federated::::n", ("", "n")),
        ("federated::g::a::b", ("g", "a::b")),
        ("federated::  g  ::n", ("g", "n")),
        ("local-1", ("", "")),
        ("federated::only", ("", "")),
        ("other::g::n", ("", "")),
        ("", ("", "")),
    ],
)
def test_a_federated_id_parses_into_its_graph_and_origin(node_id, expected):
    assert teleport.parse_federated_node_id(node_id) == expected


def test_the_id_builder_and_the_parser_agree():
    # FederationManager._build_cache builds ids with the builder, so the format
    # teleport reads a graph id back out of is the format that was written.
    built = teleport.build_federated_node_id("esam-main", "remote-1")

    assert built == "federated::esam-main::remote-1"
    assert teleport.parse_federated_node_id(built) == ("esam-main", "remote-1")


def test_a_visible_graph_may_still_report_an_unknown_node():
    # The caller is entitled to see this graph, so "no such node" tells them
    # nothing they could not already learn.
    target = _resolve(graph_access_matches=_allow_all, node_exists=False)

    assert target.status == teleport.STATUS_UNKNOWN_NODE


def test_narrowing_that_admits_another_graph_still_denies_this_one():
    target = _resolve(graph_access_matches=_allow_only("remote-b"))

    assert target.status == teleport.STATUS_PERMISSION_DENIED


def test_narrowing_that_admits_this_graph_allows_the_route():
    target = _resolve(graph_access_matches=_allow_only("remote-a"))

    assert target.status == teleport.STATUS_OK


def test_a_local_node_is_denied_when_the_local_graph_is_not_visible():
    # The local path uses the same narrowing as the federation path: a caller
    # narrowed to remote graphs only must not be told about a local node.
    target = _resolve(
        node_metadata={},
        graph_access_matches=_allow_only("remote-a"),
    )

    assert target.status == teleport.STATUS_PERMISSION_DENIED
    assert target.reason == teleport.REASON_NOT_VISIBLE


# ---------------------------------------------------------------------------
# Behaviour 2: unavailable graph
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cache_status", ["degraded", "offline", "disabled"])
def test_an_unreachable_cache_makes_the_graph_unavailable(cache_status):
    target = _resolve(cache_status=cache_status)

    assert target.status == teleport.STATUS_GRAPH_UNAVAILABLE
    assert target.reason == "graph_unreachable"
    assert target.route is None


@pytest.mark.parametrize("cache_status", ["DEGRADED", "Offline"])
def test_cache_status_is_compared_case_insensitively(cache_status):
    assert _resolve(cache_status=cache_status).status == (
        teleport.STATUS_GRAPH_UNAVAILABLE
    )


def test_a_disabled_graph_is_unavailable():
    target = _resolve(graph_config=_graph(enabled=False))

    assert target.status == teleport.STATUS_GRAPH_UNAVAILABLE
    assert target.reason == "graph_disabled"


def test_a_graph_with_no_gui_url_is_unavailable_rather_than_routed_nowhere():
    target = _resolve(graph_config=_graph(gui_url=None))

    assert target.status == teleport.STATUS_GRAPH_UNAVAILABLE
    assert target.reason == "no_gui_url_configured"
    assert target.route is None


def test_a_graph_that_is_no_longer_configured_is_unavailable():
    target = _resolve(graph_config=None)

    assert target.status == teleport.STATUS_GRAPH_UNAVAILABLE
    assert target.reason == "graph_not_configured"


def test_a_cached_node_without_an_origin_node_id_cannot_be_focused():
    target = _resolve(
        node_metadata=_federated_metadata(origin_node_id=""),
    )

    assert target.status == teleport.STATUS_GRAPH_UNAVAILABLE
    assert target.reason == "no_origin_node_id"
    assert target.route is None


def test_an_unavailable_graph_is_still_named_for_an_entitled_caller():
    target = _resolve(cache_status="offline")

    assert target.origin_graph_id == "remote-a"
    assert target.origin_graph_name == "Remote A"


def test_an_unavailable_graph_falls_back_to_the_cached_display_name():
    target = _resolve(graph_config=None)

    assert target.origin_graph_name == "Remote A"


# ---------------------------------------------------------------------------
# Behaviour 3: cross-deployment
# ---------------------------------------------------------------------------


def test_a_route_to_the_same_deployment_is_not_cross_deployment():
    target = _resolve(
        graph_config=_graph(gui_url="https://same.example/graph-a"),
        local_gui_url="https://same.example/",
    )

    assert target.status == teleport.STATUS_OK
    assert target.cross_deployment is False


def test_a_route_to_a_different_host_is_cross_deployment():
    target = _resolve(
        graph_config=_graph(gui_url="https://other.example/app"),
        local_gui_url="https://same.example/",
    )

    assert target.cross_deployment is True


def test_a_different_port_is_a_different_deployment():
    target = _resolve(
        graph_config=_graph(gui_url="https://same.example:8443/app"),
        local_gui_url="https://same.example/",
    )

    assert target.cross_deployment is True


def test_a_different_scheme_is_a_different_deployment():
    target = _resolve(
        graph_config=_graph(gui_url="http://same.example/app"),
        local_gui_url="https://same.example/",
    )

    assert target.cross_deployment is True


def test_origin_comparison_ignores_case_and_path():
    target = _resolve(
        graph_config=_graph(gui_url="https://SAME.example/a/deep/path"),
        local_gui_url="https://same.EXAMPLE/elsewhere",
    )

    assert target.cross_deployment is False


@pytest.mark.parametrize("local_gui_url", ["", "not-a-url", "/relative/only"])
def test_a_deployment_with_no_origin_of_its_own_reports_every_hop(local_gui_url):
    # A deployment with no configured public URL has no origin to compare, so
    # the UI still confirms. Defaulting to "same deployment" would drop that
    # warning exactly when it is least safe, and the only other candidate — the
    # request's own host — comes from a header the caller controls.
    target = _resolve(local_gui_url=local_gui_url)

    assert target.cross_deployment is True


@pytest.mark.parametrize(
    "gui_url",
    [
        "esam.example/app",
        "/app",
        "app",
        "localhost:8100",
        # Protocol-relative: a very common way to write a scheme-agnostic URL,
        # and still resolved against the caller's origin by the browser.
        "//esam.example/app",
        # These parse as absolute, but are not somewhere a graph lives — and the
        # route would be handed to window.open.
        "javascript://x%0aalert(document.domain)//",
        "vbscript://host/x",
        "file://host/etc/passwd",
        "ftp://host/x",
    ],
)
def test_a_gui_url_that_is_not_an_absolute_url_yields_no_route(gui_url):
    # A gui_url without a scheme and host is not a route anywhere: a browser
    # would resolve it against the caller's own origin, so the user would
    # confirm leaving for another deployment and land back on their own. It
    # degrades like any other unreachable graph rather than being handed out.
    target = _resolve(
        graph_config=_graph(gui_url=gui_url),
        local_gui_url="https://here.example/",
    )

    assert teleport.deployment_origin(gui_url) == ""
    assert target.status == teleport.STATUS_GRAPH_UNAVAILABLE
    assert target.reason == "gui_url_not_absolute"
    assert target.route is None


def test_an_ok_route_always_has_a_determinate_target_origin():
    # Because a non-absolute gui_url is refused above, cross_deployment on an
    # ok route is a real comparison rather than an unknown defaulting to true.
    target = _resolve()

    assert target.status == teleport.STATUS_OK
    assert teleport.deployment_origin(target.route) != ""


def test_the_configured_trust_level_is_reported_with_the_route():
    target = _resolve(graph_config=_graph(trust_level="external"))

    assert target.trust_level == "external"
    assert target.to_dict()["trust_level"] == "external"


# ---------------------------------------------------------------------------
# Behaviour 4: backlink
# ---------------------------------------------------------------------------


def test_a_backlink_is_built_when_this_deployment_knows_its_own_url():
    target = _resolve(
        local_gui_url="https://here.example/app",
        session_id="1111-2222-3333-4444",
    )

    assert target.backlink is not None
    assert parse_qs(urlsplit(target.backlink).query)["from_session"] == [
        "1111-2222-3333-4444"
    ]
    assert target.to_dict()["backlink"] == target.backlink


def test_no_backlink_is_invented_when_the_public_url_is_unset():
    target = _resolve(local_gui_url="", session_id="1111-2222-3333-4444")

    assert target.backlink is None
    assert "backlink" not in target.to_dict()


def test_the_route_names_the_graph_the_visitor_came_from():
    target = _resolve(local_graph_name="Our Graph")

    assert parse_qs(urlsplit(target.route).query)["from_graph"] == ["Our Graph"]


def test_no_from_graph_parameter_when_this_graph_has_no_name():
    target = _resolve(local_graph_name="")

    assert "from_graph" not in parse_qs(urlsplit(target.route).query)


def test_build_backlink_returns_none_without_a_base_url():
    assert teleport.build_backlink("", session_id="abc") is None


# ---------------------------------------------------------------------------
# Search-context preservation and route shape
# ---------------------------------------------------------------------------


def test_the_search_query_is_carried_into_the_source_graph():
    target = _resolve(search_query="classification of economic activity")

    params = parse_qs(urlsplit(target.route).query)
    assert params["q"] == ["classification of economic activity"]


def test_the_session_is_carried_so_the_remote_end_can_offer_a_way_back():
    target = _resolve(session_id="1111-2222-3333-4444")

    params = parse_qs(urlsplit(target.route).query)
    assert params["from_session"] == ["1111-2222-3333-4444"]


def test_an_empty_search_query_adds_no_parameter():
    params = parse_qs(urlsplit(_resolve(search_query="").route).query)

    assert "q" not in params


def test_existing_query_parameters_on_the_gui_url_are_preserved():
    target = _resolve(graph_config=_graph(gui_url="https://graph-a.example/?lang=sv"))

    params = parse_qs(urlsplit(target.route).query)
    assert params["lang"] == ["sv"]
    assert params["node"] == ["node-7"]


def test_a_colliding_parameter_on_the_gui_url_does_not_shadow_the_teleport_one():
    # The receiving end reads the first value of a repeated parameter, so a
    # gui_url carrying its own node=/q= must not win over the route's.
    target = _resolve(
        graph_config=_graph(gui_url="https://graph-a.example/app?node=stale&q=old"),
        search_query="fresh",
    )

    params = parse_qs(urlsplit(target.route).query)
    assert params["node"] == ["node-7"]
    assert params["q"] == ["fresh"]


def test_a_non_colliding_parameter_survives_alongside_the_teleport_ones():
    target = _resolve(
        graph_config=_graph(gui_url="https://graph-a.example/app?lang=sv&node=stale"),
    )

    params = parse_qs(urlsplit(target.route).query)
    assert params["lang"] == ["sv"]
    assert params["node"] == ["node-7"]


def test_a_blank_configured_parameter_is_not_discarded_from_the_route():
    route = teleport.build_route("https://x.example/app?blank=&x=1", "n1")

    params = parse_qs(urlsplit(route).query, keep_blank_values=True)
    assert params["blank"] == [""]
    assert params["x"] == ["1"]
    assert params["node"] == ["n1"]


@pytest.mark.parametrize(
    "base",
    ["/app", "here.example/app", "//here.example/app", "javascript://x/%0aalert(1)//"],
)
def test_build_backlink_refuses_a_base_url_that_is_not_a_web_url(base):
    # The receiving deployment would resolve a relative backlink against its
    # own origin, which is not where the visitor came from; a script URL is not
    # a way home at all.
    assert teleport.build_backlink(base, session_id="s1") is None


def test_a_backlink_addresses_no_node_even_if_its_base_url_named_one():
    # build_route owns the node parameter, so a configured base carrying its own
    # node= does not leave a stale one on the way home.
    backlink = teleport.build_backlink(
        "https://here.example/app?node=stale&lang=sv", session_id="s1"
    )

    params = parse_qs(urlsplit(backlink).query)
    assert "node" not in params
    assert params["lang"] == ["sv"]
    assert params["from_session"] == ["s1"]


def test_an_owned_parameter_is_removed_rather_than_inherited():
    route = teleport.build_route("https://x.example/app?q=old&lang=sv", "n1")

    params = parse_qs(urlsplit(route).query)
    assert "q" not in params
    assert params["lang"] == ["sv"]


@pytest.mark.parametrize(
    "url",
    [
        "javascript://x/%0aalert(1)//",
        "vbscript://host/x",
        "data://host/text/html,x",
        "file://host/share/x",
        "ftp://host/x",
        "//host/app",
    ],
)
def test_deployment_origin_rejects_anything_that_is_not_a_web_url(url):
    assert teleport.deployment_origin(url) == ""


def test_an_ipv6_origin_keeps_its_brackets():
    # The origin is only ever compared against another origin from this same
    # function, so unbracketing would not corrupt a URL — but it would let
    # host/port collide, so two different deployments could compare equal and
    # skip the UI's confirmation.
    assert teleport.deployment_origin("http://[::1]:8080/app") == "http://[::1]:8080"
    assert teleport.deployment_origin("http://[::1]:8080/") != (
        teleport.deployment_origin("http://[::1:8080]/")
    )


def test_deployment_origin_ignores_credentials_in_the_url():
    # Two URLs differing only in userinfo are the same deployment, so a
    # configured credential does not make a hop look cross-deployment.
    assert teleport.deployment_origin("https://u:p@h.example/x") == (
        teleport.deployment_origin("https://h.example/y")
    )


def test_build_route_replaces_rather_than_appends_a_colliding_parameter():
    route = teleport.build_route("https://x.example/app?node=stale", "new-7")

    assert parse_qs(urlsplit(route).query)["node"] == ["new-7"]


def test_a_fragment_on_the_gui_url_is_dropped_from_the_route():
    target = _resolve(graph_config=_graph(gui_url="https://graph-a.example/app#stale"))

    assert urlsplit(target.route).fragment == ""


def test_route_parameters_are_percent_encoded():
    target = _resolve(
        node_metadata=_federated_metadata(origin_node_id="node 7/å&b"),
        search_query="a b&c",
    )

    params = parse_qs(urlsplit(target.route).query)
    assert params["node"] == ["node 7/å&b"]
    assert params["q"] == ["a b&c"]
    assert "&b" not in urlsplit(target.route).query.replace("%26b", "")


def test_build_route_returns_empty_without_a_base_url():
    assert teleport.build_route("", "node-7") == ""


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://a.example/app", "https://a.example"),
        ("https://A.Example:8443/x", "https://a.example:8443"),
        ("http://a.example", "http://a.example"),
        ("", ""),
        ("/relative", ""),
        ("not a url", ""),
    ],
)
def test_deployment_origin_extracts_scheme_host_and_port(url, expected):
    assert teleport.deployment_origin(url) == expected


# ---------------------------------------------------------------------------
# Serialization contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,success",
    [
        (teleport.STATUS_OK, True),
        (teleport.STATUS_LOCAL, True),
        (teleport.STATUS_PERMISSION_DENIED, False),
        (teleport.STATUS_GRAPH_UNAVAILABLE, False),
        (teleport.STATUS_UNKNOWN_NODE, False),
    ],
)
def test_success_tracks_whether_there_is_somewhere_to_go(status, success):
    assert teleport.TeleportTarget(status=status).to_dict()["success"] is success


def test_every_payload_states_a_status_and_the_cross_deployment_flag():
    for target in (
        _resolve(),
        _resolve(graph_access_matches=_deny_all),
        _resolve(cache_status="offline"),
        _resolve(node_metadata={}),
        _resolve(node_metadata=None, node_exists=False),
    ):
        payload = target.to_dict()
        assert payload["status"] in {
            teleport.STATUS_OK,
            teleport.STATUS_LOCAL,
            teleport.STATUS_PERMISSION_DENIED,
            teleport.STATUS_GRAPH_UNAVAILABLE,
            teleport.STATUS_UNKNOWN_NODE,
        }
        assert isinstance(payload["cross_deployment"], bool)


def test_a_payload_never_carries_a_route_key_without_a_route():
    assert "route" not in _resolve(cache_status="offline").to_dict()
