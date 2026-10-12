"""Tests for resolving the teleport route through GraphService.

``backend/federation/tests/test_teleport.py`` covers the pure resolution
rules; this file covers the wiring: that a local node, a cached federated node
and an adopted node each resolve through the same entry point, and that the
caller's graph-access narrowing is the thing that decides visibility.
"""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.core import GraphStorage, Node, NodeType
from backend.federation import teleport
from backend.federation.config import FederationFileConfig
from backend.federation.manager import FederationManager
from backend.runtime.authorization import (
    GraphAccessNarrowing,
    GraphAuthorizationDecision,
)
from backend.service import GraphService

_GUI_URL = "https://esam.example/app"
_REMOTE_NODE = {"id": "remote-1", "type": "Actor", "name": "External Node"}


def _service(
    tmp_path,
    *,
    gui_url=_GUI_URL,
    enabled=True,
    cache_status="healthy",
    local_nodes=(),
):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    storage = GraphStorage(str(graph_file))
    if local_nodes:
        storage.add_nodes(list(local_nodes), [])

    endpoints = {"graph_json_url": "https://esam.example/graph.json"}
    if gui_url:
        endpoints["gui_url"] = gui_url

    config = FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "graphs": [
                    {
                        "graph_id": "esam-main",
                        "display_name": "eSam",
                        "enabled": enabled,
                        "trust_level": "partner",
                        "capabilities": {"allow_adopt": True},
                        "endpoints": endpoints,
                    }
                ],
            }
        }
    )

    manager = FederationManager(config)
    cache_nodes, _ = manager._build_cache(
        config.federation.graphs[0], [_REMOTE_NODE], []
    )
    manager._cache["esam-main"].nodes = cache_nodes
    manager._cache["esam-main"].status = cache_status

    return GraphService(storage, federation_manager=manager)


class _NarrowingHook:
    """Authorization hook that narrows graph visibility to ``include``."""

    def __init__(self, *include, allow_local=True, allowed=True):
        self._include = tuple(include)
        self._allow_local = allow_local
        self._allowed = allowed

    def evaluate(self, context):
        return GraphAuthorizationDecision(
            allowed=self._allowed,
            reason="" if self._allowed else "request refused",
            graph_access=GraphAccessNarrowing(
                enabled=True,
                allow_local_graph=self._allow_local,
                include_graph_ids=self._include,
            ),
        )


def _local_node():
    return Node(id="local-1", type=NodeType.ACTOR, name="Local Node")


# ---------------------------------------------------------------------------
# The federated path
# ---------------------------------------------------------------------------


def test_a_cached_federated_node_resolves_to_a_route_into_its_source_graph(tmp_path):
    result = _service(tmp_path).resolve_teleport("federated::esam-main::remote-1")

    assert result["success"] is True
    assert result["status"] == teleport.STATUS_OK
    assert result["origin_graph_id"] == "esam-main"
    assert result["origin_graph_name"] == "eSam"
    assert result["origin_node_id"] == "remote-1"
    assert result["trust_level"] == "partner"
    assert parse_qs(urlsplit(result["route"]).query)["node"] == ["remote-1"]


def test_the_route_carries_the_session_and_the_search_context(tmp_path):
    result = _service(tmp_path).resolve_teleport(
        "federated::esam-main::remote-1",
        session_id="1111-2222-3333-4444",
        search_query="external",
    )

    params = parse_qs(urlsplit(result["route"]).query)
    assert params["from_session"] == ["1111-2222-3333-4444"]
    assert params["q"] == ["external"]


def test_a_teleport_out_of_this_deployment_is_flagged_cross_deployment(tmp_path):
    result = _service(tmp_path).resolve_teleport(
        "federated::esam-main::remote-1",
        local_gui_url="https://here.example/",
    )

    assert result["cross_deployment"] is True


def test_a_teleport_within_one_deployment_is_not_flagged(tmp_path):
    result = _service(tmp_path, gui_url="https://here.example/other").resolve_teleport(
        "federated::esam-main::remote-1",
        local_gui_url="https://here.example/app",
    )

    assert result["cross_deployment"] is False


def test_a_backlink_is_offered_when_the_public_base_url_is_known(tmp_path):
    result = _service(tmp_path).resolve_teleport(
        "federated::esam-main::remote-1",
        session_id="1111-2222-3333-4444",
        local_gui_url="https://here.example/app",
    )

    assert parse_qs(urlsplit(result["backlink"]).query)["from_session"] == [
        "1111-2222-3333-4444"
    ]


def test_no_backlink_is_offered_when_the_deployment_url_is_unknown(tmp_path):
    result = _service(tmp_path).resolve_teleport("federated::esam-main::remote-1")

    assert "backlink" not in result


# ---------------------------------------------------------------------------
# The local path — same entry point, same provenance field
# ---------------------------------------------------------------------------


def test_a_local_node_resolves_as_already_being_here(tmp_path):
    service = _service(tmp_path, local_nodes=[_local_node()])

    result = service.resolve_teleport("local-1")

    assert result["success"] is True
    assert result["status"] == teleport.STATUS_LOCAL
    assert "route" not in result


def test_a_local_node_whose_id_looks_federated_is_still_local(tmp_path):
    # add_nodes lets a caller choose the id, so a local node can be created with
    # a federated-shaped one. Its own (absent) provenance decides, not the id.
    service = _service(tmp_path)
    service._storage.add_nodes(
        [
            Node(
                id="federated::esam-main::spoof",
                type=NodeType.ACTOR,
                name="Mine",
            )
        ],
        [],
    )

    result = service.resolve_teleport("federated::esam-main::spoof")

    assert result["status"] == teleport.STATUS_LOCAL, result
    assert result["origin_graph_id"] == ""
    assert "route" not in result


def test_an_unknown_id_is_neither_local_nor_routed(tmp_path):
    result = _service(tmp_path).resolve_teleport("no-such-node")

    assert result["success"] is False
    assert result["status"] == teleport.STATUS_UNKNOWN_NODE
    assert "route" not in result


def test_an_adopted_node_resolves_as_local_because_adoption_strips_provenance(tmp_path):
    # adopt_federated_node removes the federation bookkeeping keys and re-nests
    # lineage under metadata["adopted_from"], so the adopted copy is owned here.
    # Teleport must agree with that rather than routing away from the local copy.
    service = _service(tmp_path)
    adopted = service.adopt_federated_node("federated::esam-main::remote-1")
    assert adopted["success"] is True, adopted

    result = service.resolve_teleport(adopted["adopted_node"]["id"])

    assert result["status"] == teleport.STATUS_LOCAL


def test_an_adoption_reference_stub_still_resolves_to_its_source_graph(tmp_path):
    # adopt_federated_node also writes a LOCAL reference stub keyed by the
    # federated id (is_federated_reference, provenance retained) — the duality
    # search_graph dedups on. The stub lives in storage, not in the cache, so
    # resolving the graph config from the node's cache entry reported a
    # configured, healthy graph as unavailable. Both the config and the cache
    # status are keyed on origin_graph_id instead.
    service = _service(tmp_path)
    assert service.adopt_federated_node("federated::esam-main::remote-1")["success"]

    stub = service._storage.get_node("federated::esam-main::remote-1")
    assert stub is not None and stub.metadata.get("is_federated_reference") is True

    result = service.resolve_teleport("federated::esam-main::remote-1")

    assert result["status"] == teleport.STATUS_OK, result
    assert result["origin_graph_name"] == "eSam"
    assert parse_qs(urlsplit(result["route"]).query)["node"] == ["remote-1"]


def test_a_stub_resolves_even_once_the_cache_no_longer_holds_the_node(tmp_path):
    service = _service(tmp_path)
    assert service.adopt_federated_node("federated::esam-main::remote-1")["success"]
    # A restart before the first sync, or the remote dropping the node.
    service._federation_manager._cache["esam-main"].nodes = {}

    result = service.resolve_teleport("federated::esam-main::remote-1")

    assert result["status"] == teleport.STATUS_OK, result


def test_the_route_names_this_graph_so_the_far_end_can_say_where_it_came_from(
    tmp_path,
):
    result = _service(tmp_path).resolve_teleport("federated::esam-main::remote-1")

    params = parse_qs(urlsplit(result["route"]).query)
    assert params["from_graph"] == [_service(tmp_path)._storage.get_graph_name()]


# ---------------------------------------------------------------------------
# Behaviour 1: permission denial comes from the existing narrowing
# ---------------------------------------------------------------------------


def test_a_caller_narrowed_away_from_the_source_graph_gets_no_route(tmp_path):
    service = _service(tmp_path)
    service._authorization_hook = _NarrowingHook("other-graph")

    result = service.resolve_teleport("federated::esam-main::remote-1")

    assert result["status"] == teleport.STATUS_PERMISSION_DENIED
    assert "route" not in result


def test_a_denied_caller_is_told_nothing_about_the_graph(tmp_path):
    service = _service(tmp_path)
    service._authorization_hook = _NarrowingHook("other-graph")

    result = service.resolve_teleport("federated::esam-main::remote-1")

    assert result["origin_graph_id"] == ""
    assert result["origin_graph_name"] == ""
    assert "esam.example" not in repr(result)
    assert "eSam" not in repr(result)
    assert result["reason"] == teleport.REASON_NOT_VISIBLE


def test_denial_is_identical_whether_or_not_the_node_exists(tmp_path):
    # Through the real entry point, which passes no metadata for a node it did
    # not find — the shape the pure-resolver test could not reach. A differing
    # answer here is a per-node existence oracle inside a graph the caller may
    # not see, and also distinguishes a synced invisible graph from an
    # unconfigured one.
    service = _service(tmp_path)
    service._authorization_hook = _NarrowingHook("other-graph")

    existing = service.resolve_teleport("federated::esam-main::remote-1")
    missing = service.resolve_teleport("federated::esam-main::no-such-node")
    unconfigured = service.resolve_teleport("federated::never-configured::whatever")

    assert existing["status"] == teleport.STATUS_PERMISSION_DENIED
    assert existing == missing == unconfigured


def test_a_caller_narrowed_to_the_source_graph_still_gets_its_route(tmp_path):
    service = _service(tmp_path)
    service._authorization_hook = _NarrowingHook("esam-main")

    result = service.resolve_teleport("federated::esam-main::remote-1")

    assert result["status"] == teleport.STATUS_OK


def test_a_caller_scoped_to_the_remote_graph_alone_still_gets_its_route(tmp_path):
    # A SaaS caller may be entitled to a federated graph and not to the host's
    # own local graph. The pre-check that avoids reading config for an excluded
    # graph must test the node's graph, not the local one, or such a caller is
    # told a healthy, configured, entitled graph is unreachable.
    service = _service(tmp_path)
    service._authorization_hook = _NarrowingHook("esam-main", allow_local=False)

    result = service.resolve_teleport("federated::esam-main::remote-1")

    assert result["status"] == teleport.STATUS_OK, result
    assert result["route"].startswith("https://esam.example/app")


def test_provenance_alone_classifies_a_node_as_remote(tmp_path):
    # access.node_graph_id narrows visibility on origin_graph_id alone, so a node
    # that carries provenance without the is_federated marker — a profile seed,
    # an import, or a node created through MCP/REST — is already remote for
    # access control. Teleport must agree rather than reading a second marker.
    service = _service(tmp_path)
    service._storage.add_nodes(
        [
            Node(
                id="seeded-1",
                type=NodeType.ACTOR,
                name="Seeded",
                metadata={
                    "origin_graph_id": "esam-main",
                    "origin_graph_name": "eSam",
                    "origin_node_id": "remote-1",
                },
            )
        ],
        [],
    )

    result = service.resolve_teleport("seeded-1")

    assert result["status"] == teleport.STATUS_OK, result
    assert parse_qs(urlsplit(result["route"]).query)["node"] == ["remote-1"]


def test_nothing_about_an_excluded_graph_is_even_looked_up(tmp_path):
    # The payload is identical either way because the resolver denies before it
    # reads anything, so the property is only observable as a call that does not
    # happen.
    service = _service(tmp_path)
    service._authorization_hook = _NarrowingHook("other-graph")
    manager = service._federation_manager
    looked_up = []
    real_config = manager.get_graph_config
    real_status = manager.get_cache_status
    manager.get_graph_config = lambda gid: (
        looked_up.append(("config", gid)) or real_config(gid)
    )
    manager.get_cache_status = lambda gid: (
        looked_up.append(("status", gid)) or real_status(gid)
    )

    result = service.resolve_teleport("federated::esam-main::remote-1")

    assert result["status"] == teleport.STATUS_PERMISSION_DENIED
    assert looked_up == []


def test_a_local_node_is_withheld_from_a_caller_narrowed_off_the_local_graph(tmp_path):
    service = _service(tmp_path, local_nodes=[_local_node()])
    service._authorization_hook = _NarrowingHook("esam-main", allow_local=False)

    result = service.resolve_teleport("local-1")

    assert result["status"] == teleport.STATUS_PERMISSION_DENIED


def test_a_refused_request_is_an_access_denied_error_not_a_teleport_status(tmp_path):
    # A request-level refusal must stay the shape the REST layer turns into a
    # 403; only the per-graph case is a teleport status the UI renders.
    service = _service(tmp_path)
    service._authorization_hook = _NarrowingHook(allowed=False)

    result = service.resolve_teleport("federated::esam-main::remote-1")

    assert result["error_code"] == "access_denied"
    assert "status" not in result


def _two_graph_service(tmp_path):
    """A service with two configured graphs, both cached and healthy."""
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    storage = GraphStorage(str(graph_file))

    config = FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "graphs": [
                    {
                        "graph_id": "esam-main",
                        "display_name": "eSam",
                        "trust_level": "partner",
                        "endpoints": {
                            "graph_json_url": "https://esam.example/graph.json",
                            "gui_url": "https://esam.example/app",
                        },
                    },
                    {
                        "graph_id": "other-main",
                        "display_name": "Other",
                        "trust_level": "external",
                        "endpoints": {
                            "graph_json_url": "https://other.example/graph.json",
                            "gui_url": "https://other.example/app",
                        },
                    },
                ],
            }
        }
    )
    manager = FederationManager(config)
    for graph, node_id in zip(config.federation.graphs, ("remote-1", "other-1")):
        cache_nodes, _ = manager._build_cache(
            graph, [{"id": node_id, "type": "Actor", "name": node_id}], []
        )
        manager._cache[graph.graph_id].nodes = cache_nodes
        manager._cache[graph.graph_id].status = "healthy"
    return GraphService(storage, federation_manager=manager)


def test_a_node_routes_to_its_own_graph_not_to_another_configured_one(tmp_path):
    # Every other fixture configures a single graph, which cannot tell a
    # correctly-keyed config lookup from one that ignores the graph id and
    # returns whichever graph comes first.
    service = _two_graph_service(tmp_path)

    first = service.resolve_teleport("federated::esam-main::remote-1")
    second = service.resolve_teleport("federated::other-main::other-1")

    assert first["route"].startswith("https://esam.example/app")
    assert first["origin_graph_name"] == "eSam"
    assert first["trust_level"] == "partner"
    assert second["route"].startswith("https://other.example/app")
    assert second["origin_graph_name"] == "Other"
    assert second["trust_level"] == "external"


def test_no_other_graphs_endpoint_appears_for_a_narrowed_caller(tmp_path):
    service = _two_graph_service(tmp_path)
    service._authorization_hook = _NarrowingHook("esam-main")

    allowed = service.resolve_teleport("federated::esam-main::remote-1")
    denied = service.resolve_teleport("federated::other-main::other-1")

    assert allowed["status"] == teleport.STATUS_OK
    assert "other.example" not in repr(allowed)
    assert denied["status"] == teleport.STATUS_PERMISSION_DENIED
    assert "other.example" not in repr(denied)
    assert "Other" not in repr(denied)


# ---------------------------------------------------------------------------
# Federation disabled globally
# ---------------------------------------------------------------------------


def test_a_cached_node_does_not_route_while_federation_is_disabled(tmp_path):
    # Every other federation path gates on the global flag; a cache left healthy
    # from before the flag was turned off must not keep teleport working.
    service = _service(tmp_path)
    service._federation_manager._config.federation.enabled = False

    result = service.resolve_teleport("federated::esam-main::remote-1")

    assert result["status"] == teleport.STATUS_GRAPH_UNAVAILABLE
    assert result["reason"] == "graph_not_configured"
    assert "route" not in result


def test_a_local_node_still_resolves_while_federation_is_disabled(tmp_path):
    service = _service(tmp_path, local_nodes=[_local_node()])
    service._federation_manager._config.federation.enabled = False

    assert service.resolve_teleport("local-1")["status"] == teleport.STATUS_LOCAL


# ---------------------------------------------------------------------------
# Behaviour 2: unavailable graph
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cache_status", ["degraded", "offline"])
def test_an_unreachable_source_graph_yields_no_route(tmp_path, cache_status):
    result = _service(tmp_path, cache_status=cache_status).resolve_teleport(
        "federated::esam-main::remote-1"
    )

    assert result["status"] == teleport.STATUS_GRAPH_UNAVAILABLE
    assert result["reason"] == "graph_unreachable"
    assert "route" not in result


def test_a_source_graph_without_a_gui_url_yields_no_route(tmp_path):
    result = _service(tmp_path, gui_url=None).resolve_teleport(
        "federated::esam-main::remote-1"
    )

    assert result["status"] == teleport.STATUS_GRAPH_UNAVAILABLE
    assert result["reason"] == "no_gui_url_configured"


def test_an_unavailable_graph_is_still_named_for_an_entitled_caller(tmp_path):
    result = _service(tmp_path, cache_status="offline").resolve_teleport(
        "federated::esam-main::remote-1"
    )

    assert result["origin_graph_name"] == "eSam"


def test_the_cache_status_the_manager_reports_is_the_one_teleport_reads(tmp_path):
    service = _service(tmp_path, cache_status="degraded")

    assert service._federation_manager.get_cache_status("esam-main") == "degraded"
    assert service.resolve_teleport("federated::esam-main::remote-1")["status"] == (
        teleport.STATUS_GRAPH_UNAVAILABLE
    )


def test_cache_status_is_empty_for_a_graph_that_is_not_configured(tmp_path):
    service = _service(tmp_path)

    assert service._federation_manager.get_cache_status("no-such-graph") == ""


# ---------------------------------------------------------------------------
# Federation disabled / absent
# ---------------------------------------------------------------------------


def test_a_local_node_still_resolves_with_no_federation_manager_at_all(tmp_path):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    storage = GraphStorage(str(graph_file))
    storage.add_nodes([_local_node()], [])
    service = GraphService(storage)

    assert service.resolve_teleport("local-1")["status"] == teleport.STATUS_LOCAL


def test_a_federated_id_is_unknown_with_no_federation_manager(tmp_path):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    service = GraphService(GraphStorage(str(graph_file)))

    assert service.resolve_teleport("federated::esam-main::remote-1")["status"] == (
        teleport.STATUS_UNKNOWN_NODE
    )
