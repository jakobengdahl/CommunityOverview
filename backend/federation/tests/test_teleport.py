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
        "request_origin": "https://graph-a.example/",
        "session_id": "",
        "search_query": "",
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


def test_an_empty_origin_graph_id_is_treated_as_local_not_as_a_remote_graph():
    target = _resolve(node_metadata={"origin_graph_id": "   "})

    assert target.status == teleport.STATUS_LOCAL


def test_an_unknown_node_is_reported_as_unknown_rather_than_local():
    target = _resolve(node_metadata=None, node_exists=False)

    assert target.status == teleport.STATUS_UNKNOWN_NODE
    assert target.to_dict()["success"] is False


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
    assert target.reason == "source_graph_not_visible"


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
    assert target.reason == "local_graph_not_visible"


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
        request_origin="https://same.example/",
    )

    assert target.status == teleport.STATUS_OK
    assert target.cross_deployment is False


def test_a_route_to_a_different_host_is_cross_deployment():
    target = _resolve(
        graph_config=_graph(gui_url="https://other.example/app"),
        request_origin="https://same.example/",
    )

    assert target.cross_deployment is True


def test_a_different_port_is_a_different_deployment():
    target = _resolve(
        graph_config=_graph(gui_url="https://same.example:8443/app"),
        request_origin="https://same.example/",
    )

    assert target.cross_deployment is True


def test_a_different_scheme_is_a_different_deployment():
    target = _resolve(
        graph_config=_graph(gui_url="http://same.example/app"),
        request_origin="https://same.example/",
    )

    assert target.cross_deployment is True


def test_origin_comparison_ignores_case_and_path():
    target = _resolve(
        graph_config=_graph(gui_url="https://SAME.example/a/deep/path"),
        request_origin="https://same.EXAMPLE/elsewhere",
    )

    assert target.cross_deployment is False


@pytest.mark.parametrize("request_origin", ["", "not-a-url", "/relative/only"])
def test_an_unknown_caller_origin_is_reported_as_cross_deployment(request_origin):
    # The UI warns before leaving; defaulting to "same deployment" when we
    # cannot tell would drop that warning exactly when it is least safe.
    target = _resolve(request_origin=request_origin)

    assert target.cross_deployment is True


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
